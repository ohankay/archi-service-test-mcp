"""Traffic log -> draft operation definitions (and, if asked, the user's own secrets into the local secrets.yaml).

The server reads the log; the result for the agent is a summary without values. For each operation of a defined
system that is not yet defined, a draft ``operation`` is proposed:

* fields of the envelope, credentials and customer fields are left out (the system definition supplies them;
  customer fields become ``{{customer.no}}``),
* dates become inputs with a format and a default relative to the log's day (21.10 in a log of 28.10 -> today-7),
* numbers that identify something (account no ...) and free text become inputs with the log's value as default,
* flags (true/false), screen codes (MainResource*) and zero amounts become fixed fields (``fixed``).

The draft is a ``draft``: the user (with the agent) gives it a friendly name, decides which inputs matter and which
checks it needs. Responses are not taken from logs on purpose: some app logs label concurrent responses with the
wrong method; what a response looks like is learned from a real call.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..importer.analyze import Importer, guess_kind
from ..importer.parsers import parse_traffic
from ..normalize import json_ready
from . import secrets
from .store import DefinitionError
from .values import day_text

if TYPE_CHECKING:
    from ..engine import Engine

_DATES = (("%d.%m.%Y", "DD.MM.YYYY", re.compile(r"^\d{2}\.\d{2}\.\d{4}$")),
          ("%Y-%m-%d", "YYYY-MM-DD", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
          ("%d/%m/%Y", "DD/MM/YYYY", re.compile(r"^\d{2}/\d{2}/\d{4}$")))


def _log_day(ts: str | None) -> _dt.date | None:
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(str(ts or "")[:19], fmt).date()
        except ValueError:
            continue
    return None


def _input_name(field: str) -> str:
    """AccountNumber -> account_number (readable, stable; the user may rename it)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", field).lower()


def _input(field: str, value: Any, day: _dt.date | None) -> tuple[str, dict[str, Any]] | None:
    """A sample value -> (input name, definition), or None when it belongs to 'fixed'."""
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        return None
    if "resource" in field.lower():
        return None
    if isinstance(value, str):
        for fmt, date_format, rx in _DATES:
            if rx.match(value):
                default: Any = value
                try:
                    when = _dt.datetime.strptime(value, fmt).date()
                    if day:
                        default = day_text((when - day).days)
                except ValueError:
                    pass
                return _input_name(field), {"field": field, "type": "date", "format": date_format, "default": default}
        if not value.strip():
            return None
        return _input_name(field), {"field": field, "type": "text", "default": value}
    if isinstance(value, int):
        if value == 0:
            return None
        return _input_name(field), {"field": field, "type": "integer", "default": value}
    return _input_name(field), {"field": field, "type": "decimal", "default": json_ready(value)}


class LogImporter:
    def __init__(self, engine: "Engine") -> None:
        self.e = engine
        self.store = engine.store

    def run(self, path: str | None = None, content: str | None = None, environment: str | None = None,
            apply: bool = False, save_secrets: bool = False) -> dict[str, Any]:
        systems = [r for r in self.store.all("system") if r.name in self.e.profiles]
        if not systems:
            raise DefinitionError(["No (valid) system is defined. A system definition is needed first "
                                   "(definitions/systems/...)."])
        imp = Importer(self.e)
        if environment:
            self.e.settings.environment(environment)
        text, source = imp._read(path, content)
        fmt, exchanges, warnings = parse_traffic(text)
        bases = [b for name in (r.name for r in systems) for b in imp._bases(name)]
        bases.sort(key=lambda x: -len(x[2]))
        by_op: dict[tuple[str, str], list[Any]] = {}
        envs: Counter = Counter()
        outside: Counter = Counter()
        noise: Counter = Counter()
        order: list[tuple[str, str]] = []
        for ex in exchanges:
            loc = imp._locate(ex, bases, None, None)
            if loc is None:
                parts = urlsplit(ex.url)
                outside[f"{parts.hostname}{'/'.join(parts.path.split('/')[:3])}"] += 1
                continue
            system_name, env, rel = loc
            envs[env] += 1
            profile = self.e.profiles[system_name].profile
            split = imp._split(profile, rel)
            if split is None:
                continue
            op, op_path = split
            if profile.is_noise(op):
                noise[op] += 1
                continue
            key = (system_name, op)
            if key not in by_op:
                order.append(key)
            by_op.setdefault(key, []).append((ex, op_path, env))
        if not by_op and exchanges:
            hosts = sorted({urlsplit(x.url).hostname or "?" for x in exchanges})
            warnings.append("no address in the log matches an environment address of a system: " + ", ".join(hosts)
                            + ". Check the 'address' of that environment in the system definition.")
        env = environment or (envs.most_common(1)[0][0] if envs else None)
        report: dict[str, Any] = {"format": fmt, "source": source, "requests": len(exchanges),
                                  "environment": env, "environments": dict(envs)}
        new, known, drafts = [], [], []
        for system_name, op in order:
            samples = by_op[(system_name, op)]
            system = self.store.get("system", system_name)
            profile = self.e.profiles[system_name].profile
            login_ops = {s.operation for s in profile.session.login}
            if op in login_ops or (profile.session.logout and op == profile.session.logout.operation):
                continue
            existing = self.store.find("operation", op)
            if existing is not None:
                defined = {i["field"] for i in (existing.data.get("inputs") or {}).values()} | \
                    set(existing.data.get("fixed") or {})
                skip = profile.envelope_keys() | set(profile.secret_fields) | set(profile.test_customer_fields)
                body = samples[-1][0].request_body if isinstance(samples[-1][0].request_body, dict) else {}
                new_fields = sorted(k for k in body if k not in defined and k not in skip)
                entry = {"operation": existing.name, "method": op}
                if new_fields:
                    entry["fields_not_in_definition"] = new_fields
                known.append(entry)
                continue
            draft = self._draft(system.name, op, samples, profile, source)
            drafts.append(draft)
            new.append({"method": op, "suggested_name": draft["name"], "kind": draft["kind"],
                        "inputs": {k: f"{i['type']}" + (f" (default {i['default']})" if "default" in i else "")
                                   for k, i in (draft.get("inputs") or {}).items()},
                        "fixed_fields": len(draft.get("fixed") or {})})
        report["new_operations"] = new
        if known:
            report["already_defined"] = known
        if noise:
            report["noise_skipped"] = dict(noise.most_common())
        if outside:
            report["outside_systems"] = dict(outside.most_common(10))
        warnings.append("Responses are not taken from logs: some app logs write the responses of concurrent calls "
                        "under the wrong method. Response fields are seen on the first real call.")
        report["warnings"] = warnings
        if save_secrets:
            report["secrets"] = self._secrets(exchanges, imp, bases, env)
        if apply:
            saved = [str(self.store.save("operation", draft).path) for draft in drafts]
            report["saved"] = saved
            self.e.reload_definitions()
        report["next_step"] = (
            "Preview. Show the user the new operations; ask for friendly names and which inputs matter, and if they "
            "approve save them as drafts with apply=true." if not apply else
            "Drafts saved (status: draft). Settle their names and inputs with the user and update them with "
            "save_definition; try them with a real call (call_operation).")
        return report

    def _draft(self, system: str, op: str, samples: list[Any], profile: Any, source: str) -> dict[str, Any]:
        ex, op_path, _ = samples[-1]
        body = ex.request_body if isinstance(ex.request_body, dict) else {}
        day = _log_day(ex.timestamp)
        skip = profile.envelope_keys() | set(profile.secret_fields)
        customer_fields = set(profile.test_customer_fields)
        masker = self.e.masker
        inputs: dict[str, Any] = {}
        fixed: dict[str, Any] = {}
        for field, value in body.items():
            if field in skip or masker._field_kind(field) == "secret":
                continue
            if field in customer_fields:
                fixed[field] = "{{customer.no}}"
                continue
            found = _input(field, value, day)
            if found is None:
                fixed[field] = json_ready(value)
                continue
            name, spec = found
            if masker._field_kind(field) == "partial" or masker.mask(spec.get("default"), strict=True) != \
                    spec.get("default"):
                spec.pop("default", None)          # personal data is not written into definitions
                spec["required"] = True
            inputs[name] = spec
        kind = guess_kind(op, set(), None)
        draft: dict[str, Any] = {"name": op, "description": f"Made from the log {source}; review its name and inputs.",
                                 "system": system, "method": op, "path": op_path,
                                 "kind": kind if kind in ("read", "mutating", "auth") else "unknown",
                                 "session": True, "status": "draft", "source_log": Path(source).name}
        if ex.method and ex.method != "POST":
            draft["http_method"] = ex.method
        if inputs:
            draft["inputs"] = inputs
        if fixed:
            draft["fixed"] = fixed
        return draft

    def _secrets(self, exchanges: list[Any], imp: Importer, bases: list, env: str | None) -> Any:
        if not env:
            return {"saved": [], "note": "environment not found; no secret value saved"}
        names: list[str] = []
        values: dict[str, Any] = {}
        for system in self.store.all("system"):
            for name, where in (system.data.get("secret_values") or {}).items():
                for ex in exchanges:
                    if (ex.name or "") != where["method"] or not isinstance(ex.request_body, dict):
                        continue
                    loc = imp._locate(ex, bases, None, None)
                    if loc is None or loc[1] != env:
                        continue
                    value = ex.request_body.get(where["field"])
                    if value not in (None, ""):
                        values[name] = value
                        self.e.masker.add_secret(str(value), name, credential=True)
                        names.append(name)
                        break
        if not values:
            return {"saved": [], "note": "the secret_values of the system definition were not found in the log"}
        path = secrets.save(env, values)
        self.e.reload_secrets()
        return {"saved": sorted(set(names)), "environment": env, "file": str(path),
                "note": "The values were written only to secrets.yaml on this computer; they appear in no output."}
