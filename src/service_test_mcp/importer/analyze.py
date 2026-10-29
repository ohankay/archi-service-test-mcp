"""Traffic log -> catalog proposal (and optionally a draft scenario).

The flow (all deterministic, inside the MCP server):

1. parse the log (``parsers``) and drop exchanges that are noise for the profile (polling, client logs ...),
2. find the service and environment of each exchange from its URL: the host and base path are matched against
   ``base_url`` of the profile services in services.yaml (an unknown host is reported, never guessed),
3. split each request body into the profile's envelope and the operation's own params,
4. classify fields: credentials (never written anywhere), test_customer fields (test customer data), values that came
   from an earlier response (session / chaining dependencies), and the rest (user input),
5. infer request and response JSON schemas, merge them with the existing catalog entry and report what changed,
6. propose a kind (read / mutating / auth / unknown) and whether the operation needs a session (it does when the
   app sent it with a value - e.g. the session key in the envelope - that a login response had issued),
7. ``apply=True`` writes the catalog; hand-made fields (description, kind, tags, session) are kept.

The result for the model is a summary: operation names, statuses, field names and masked examples - no raw
values from the log.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..config import ConfigError
from ..normalize import json_ready
from ..profile.model import Catalog, Operation, Profile
from . import schema as sch
from .parsers import MAX_LOG_BYTES, Exchange, parse_traffic

if TYPE_CHECKING:
    from ..engine import Engine

READ_PREFIXES = ("get", "list", "has", "is", "check", "search", "query", "find", "read", "fetch", "load", "show",
                 "inquire", "inquiry", "calculate", "validate", "can", "count", "download", "export", "preview")
MUTATING_PREFIXES = ("insert", "update", "delete", "remove", "add", "create", "save", "set", "send", "transfer",
                     "pay", "execute", "approve", "confirm", "cancel", "complete", "submit", "register", "start",
                     "end", "close", "change", "upload", "assign", "activate", "deactivate", "block", "unblock",
                     "apply", "buy", "sell", "order", "revoke", "reset", "mark", "chat", "post", "put")
AUTH_WORDS = ("login", "logout", "logon", "logoff", "signin", "signout", "otp", "authenticate", "token")
MIN_DEPENDENCY_LEN = 8


@dataclass
class Match:
    exchange: Exchange
    service: str
    environment: str
    operation: str
    path: str


@dataclass
class OpAccumulator:
    name: str
    path: str = ""
    method: str = "POST"
    samples: int = 0
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    last_params: dict[str, Any] = field(default_factory=dict)
    environments: set[str] = field(default_factory=set)
    with_session: int = 0                  # samples that carried a value issued by an earlier login response
    test_customer_fields: set[str] = field(default_factory=set)
    credential_fields: set[str] = field(default_factory=set)
    dependencies: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))


def guess_kind(name: str, login_ops: set[str], logout_op: str | None) -> str:
    if name in login_ops or name == logout_op:
        return "auth"
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name) or [name]
    first = words[0].lower()
    if first in READ_PREFIXES:
        return "read"                      # GetLoginCommunicationList is a read, not a login
    if first in MUTATING_PREFIXES:
        return "mutating"
    if any(w in name.lower() for w in AUTH_WORDS):
        return "auth"
    return "unknown"


def _leaves(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += _leaves(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value[:20]):
            out += _leaves(v, f"{path}[{i}]")
    else:
        out.append((path, value))
    return out


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[*]", path)


def _dependency_value(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    return text if len(text) >= MIN_DEPENDENCY_LEN else None


def _dependency_list(deps: dict[str, Counter]) -> list[dict[str, Any]]:
    """[{field, from}] - field names are values here, so the masker (which hides *by key*) leaves them readable."""
    return [{"field": f[2:] if f.startswith("$.") else f, "from": [src for src, _ in c.most_common(3)]}
            for f, c in sorted(deps.items())]


class Importer:
    def __init__(self, engine: "Engine") -> None:
        self.engine = engine

    # -- step 2: which service / environment -----------------------------------------------------------------------
    def _bases(self, service: str | None) -> list[tuple[str, str, str]]:
        """(service, environment, base_url) for profile services, longest base first."""
        settings = self.engine.settings
        names = [self.engine.settings.service(service).name] if service else list(self.engine.profiles)
        out = []
        for name in names:
            if name not in self.engine.profiles:
                raise ConfigError(f"'{name}' has no profile; import_traffic_log needs a profile service")
            svc = settings.services[name]
            for env in settings.environments:
                base = svc.value_for("base_url", env)
                if base and (not svc.environments or env in svc.environments):
                    out.append((name, env, base.rstrip("/")))
        return sorted(out, key=lambda x: -len(x[2]))

    def _locate(self, ex: Exchange, bases: list[tuple[str, str, str]], service: str | None,
                environment: str | None) -> tuple[str, str, str] | None:
        url = ex.url.split("?")[0]
        for name, env, base in bases:
            if url.lower().startswith(base.lower() + "/"):
                return name, env, url[len(base) + 1:]
        if service and environment:
            # host not in services.yaml for this environment: accept by base path, but say so in the report
            for name, env, base in bases:
                if name != self.engine.settings.service(service).name or env != environment:
                    continue
                base_path = urlsplit(base).path.rstrip("/")
                path = urlsplit(url).path
                if base_path and path.lower().startswith(base_path.lower() + "/"):
                    return name, env, path[len(base_path) + 1:]
        return None

    @staticmethod
    def _split(profile: Profile, rel: str) -> tuple[str, str] | None:
        pattern = re.escape(profile.url.strip("/")).replace(re.escape("{path}"), "(?P<path>.*?)") \
            .replace(re.escape("{operation}"), "(?P<operation>[^/]+)")
        m = re.fullmatch(pattern, rel.strip("/"))
        if not m:
            return None
        return m.group("operation"), (m.groupdict().get("path") or "").strip("/")

    # -- main -----------------------------------------------------------------------------------------------------
    def run(self, path: str | None = None, content: str | None = None, service: str | None = None,
            environment: str | None = None, apply: bool = False, scenario_path: str | None = None,
            fmt: str | None = None, include_noise: bool = False) -> dict[str, Any]:
        engine = self.engine
        engine._require_config()
        if not engine.profiles:
            broken = {k: v for k, v in engine.settings.broken.items() if v.startswith("profile:")}
            raise ConfigError("No profile services configured: add 'profile:' to a REST service in services.yaml"
                              + (f". Broken profiles: {broken}" if broken else ""))
        if environment:
            engine.settings.environment(environment)  # validates (and refuses production names)
        text, source = self._read(path, content)
        fmt, exchanges, warnings = parse_traffic(text, fmt)
        bases = self._bases(service)

        matches: list[Match] = []
        outside: Counter = Counter()
        noise: Counter = Counter()
        host_fallback: Counter = Counter()
        env_conflicts: Counter = Counter()
        for ex in exchanges:
            loc = self._locate(ex, bases, service, environment)
            if loc is None:
                parts = urlsplit(ex.url)
                outside[f"{parts.hostname}{'/'.join(parts.path.split('/')[:3])}"] += 1
                continue
            name, env, rel = loc
            if environment and env != environment:
                env_conflicts[f"{urlsplit(ex.url).hostname} -> {env}"] += 1
                env = environment
            if not any(ex.url.lower().startswith(b.lower()) for n, e, b in bases if n == name and e == env):
                host_fallback[urlsplit(ex.url).hostname or "?"] += 1
            profile = engine.profiles[name].profile
            split = self._split(profile, rel)
            if split is None:
                outside[f"{name}: {rel[:60]} (does not fit request.url {profile.url})"] += 1
                continue
            op, op_path = split
            if profile.is_noise(op) and not include_noise:
                noise[op] += 1
                continue
            matches.append(Match(ex, name, env, op, op_path))
        if host_fallback:
            warnings.append("host(s) not in services.yaml for the given environment, matched by base path: "
                            + ", ".join(f"{h} ({n})" for h, n in host_fallback.items()))
        if env_conflicts:
            warnings.append("the hosts point to another environment than the one given; the given one was used: "
                            + ", ".join(f"{k} ({n})" for k, n in env_conflicts.items()))
        if not matches and exchanges:
            hint = ("no exchange matched a profile service. Hosts in the log: "
                    + ", ".join(sorted({urlsplit(e.url).hostname or '?' for e in exchanges}))
                    + ". Add the host to the service's base_url for its environment in services.yaml, or give "
                      "'service' and 'environment' to accept it by base path.")
            warnings.append(hint)

        reports = []
        for name in sorted({m.service for m in matches}):
            reports.append(self._service_report(name, [m for m in matches if m.service == name], source, apply,
                                                scenario_path))
        out: dict[str, Any] = {"format": fmt, "source": source, "exchanges": len(exchanges),
                               "matched": len(matches), "applied": bool(apply and reports), "services": reports}
        if noise:
            out["noise_skipped"] = dict(noise.most_common())
        if outside:
            out["outside_profile_services"] = dict(outside.most_common(15))
        if warnings:
            out["warnings"] = warnings
        out["next_steps"] = self._next_steps(reports, apply)
        return out

    def _read(self, path: str | None, content: str | None) -> tuple[str, str]:
        if content is not None:
            return content, "(inline)"
        if not path:
            raise ValueError("give 'path' (log file) or 'content'")
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Log file not found: {p}")
        if p.stat().st_size > MAX_LOG_BYTES:
            raise ValueError(f"Log file is larger than {MAX_LOG_BYTES // (1024 * 1024)} MB; split it")
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "cp1254", "latin-1"):
            try:
                return raw.decode(enc), p.name
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace"), p.name

    # -- per service ------------------------------------------------------------------------------------------------
    def _service_report(self, name: str, matches: list[Match], source: str, apply: bool,
                        scenario_path: str | None) -> dict[str, Any]:
        engine = self.engine
        runtime = engine.profiles[name]
        profile = runtime.profile
        masker = engine.masker
        envelope_keys = profile.envelope_keys()
        login_ops = {s.operation for s in profile.session.login}
        logout_op = profile.session.logout.operation if profile.session.logout else None
        secret_names = {f.lower() for f in profile.secret_fields}

        def is_secret(key: str) -> bool:
            return key.lower() in secret_names or masker._field_kind(key) == "secret"

        # credentials and tokens must never leave the server, whatever field they show up in later
        for m in matches:
            for body in (m.exchange.request_body, m.exchange.response_body):
                for p, v in _leaves(body):
                    if isinstance(v, str) and is_secret(p.rsplit(".", 1)[-1]):
                        masker.add_secret(v, p.rsplit(".", 1)[-1], credential=True)

        produced: dict[str, tuple[str, str]] = {}   # value -> (operation, generic JSONPath) of the latest response
        accs: dict[str, OpAccumulator] = {}
        envelope_deps: dict[str, Counter] = defaultdict(Counter)
        key_presence: Counter = Counter()
        bodies = 0
        order: list[Match] = []
        for m in matches:
            ex = m.exchange
            acc = accs.setdefault(m.operation, OpAccumulator(name=m.operation))
            acc.path, acc.method = m.path, ex.method or acc.method
            acc.samples += 1
            acc.environments.add(m.environment)
            body = ex.request_body if isinstance(ex.request_body, dict) else {}
            if isinstance(ex.request_body, dict):
                bodies += 1
                key_presence.update(set(body))
            carried_session = False
            for path, value in _leaves(body):
                key = path.split(".")[1]
                dep = _dependency_value(value)
                if dep is not None and dep in produced:
                    src_op, src_path = produced[dep]
                    target = envelope_deps if key in envelope_keys else acc.dependencies
                    target[_generic(path)][f"{src_op} {src_path}"] += 1
                    carried_session = carried_session or (key in envelope_keys and src_op in login_ops)
                if key in envelope_keys:
                    continue
                if is_secret(key):
                    acc.credential_fields.add(key)
                if key in profile.test_customer_fields:
                    acc.test_customer_fields.add(key)
            if carried_session:
                acc.with_session += 1
            clean = {k: v for k, v in body.items() if k not in envelope_keys and not is_secret(k)}
            acc.request_schema = sch.merge(acc.request_schema, sch.infer(clean))
            acc.last_params = clean
            if ex.has_response and ex.response_body is not None:
                acc.response_schema = sch.merge(acc.response_schema, sch.infer(ex.response_body))
                for path, value in _leaves(ex.response_body):
                    dep = _dependency_value(value)
                    if dep is not None:
                        produced[dep] = (m.operation, _generic(path))
            if m.operation not in login_ops and m.operation != logout_op:
                order.append(m)

        catalog = profile.catalog
        today = _dt.date.today().isoformat()
        operations = []
        updated: dict[str, Operation] = dict(catalog.operations)
        for op_name in sorted(accs):
            acc = accs[op_name]
            old = catalog.get(op_name)
            example = self._masked_example(acc.last_params, profile)
            guess = guess_kind(op_name, login_ops, logout_op)
            # needs a login when the app sent it with a session value from a login response (envelope)
            session_guess = acc.with_session > 0 and op_name not in login_ops
            entry: dict[str, Any] = {"operation": op_name, "samples": acc.samples}
            if old is None:
                entry["status"] = "new"
                new = Operation(name=op_name, path=acc.path, kind=guess, session=session_guess,
                                params=example, request_schema=acc.request_schema,
                                response_schema=acc.response_schema)
                if acc.method != profile.method:
                    new.method = acc.method
            else:
                changes = sch.diff(old.request_schema, acc.request_schema, "request") + \
                    sch.diff(old.response_schema, acc.response_schema, "response")
                if old.path != acc.path:
                    changes.insert(0, f"path: {old.path} -> {acc.path}")
                entry["status"] = "changed" if changes else "unchanged"
                if changes:
                    entry["changes"] = changes[:15] + ([f"... +{len(changes) - 15} more"] if len(changes) > 15 else [])
                new = Operation(name=old.name, path=acc.path or old.path,
                                kind=old.kind if old.kind != "unknown" else guess, session=old.session,
                                method=old.method, description=old.description, tags=old.tags,
                                params=example or old.params,
                                request_schema=sch.merge(old.request_schema, acc.request_schema),
                                response_schema=sch.merge(old.response_schema, acc.response_schema), seen=old.seen)
            seen = dict(new.seen)
            seen["samples"] = int(seen.get("samples", 0)) + acc.samples
            seen["environments"] = sorted(set(seen.get("environments", [])) | acc.environments)
            seen["last_imported"] = today
            sources = [s for s in seen.get("sources", []) if s != source] + [source]
            seen["sources"] = sources[-5:]
            new.seen = seen
            updated[new.name] = new
            entry.update(kind=new.kind, session=new.session, path=new.path)
            if new.kind == "unknown":
                entry["kind_note"] = "could not classify: set kind (read / mutating / auth) in the catalog"
            if acc.test_customer_fields:
                entry["test_customer_fields"] = sorted(acc.test_customer_fields)
            if acc.credential_fields:
                entry["credential_fields"] = sorted(acc.credential_fields)
            if acc.dependencies:
                entry["values_from_earlier_responses"] = _dependency_list(acc.dependencies)
            entry["params"] = sorted(example)
            operations.append(entry)

        common = sorted(k for k, n in key_presence.items()
                        if bodies >= 3 and n / bodies >= 0.8 and k not in envelope_keys)
        report: dict[str, Any] = {
            "service": name, "profile": profile.name,
            "environments": dict(Counter(m.environment for m in matches)),
            "catalog": str(catalog.path) if catalog.path else None,
            "summary": dict(Counter(o["status"] for o in operations)),
            "operations": operations,
        }
        if envelope_deps:
            report["envelope_values_from_responses"] = _dependency_list(envelope_deps)
        if common:
            report["envelope_candidates"] = common
            report["envelope_note"] = ("these fields are in (almost) every request but not in the profile's "
                                       "request.envelope; consider moving them there")
        if apply:
            if catalog.path is None:
                raise ConfigError(f"profile of '{name}' has no 'catalog' file to write")
            Catalog(path=catalog.path, operations=updated).save()
            profile.catalog = Catalog.load(catalog.path)
            report["written"] = str(catalog.path)
        draft = self._draft(name, profile, order, source)
        if draft:
            if scenario_path:
                report["draft_scenario_file"] = self._write_scenario(scenario_path, draft)
            else:
                report["draft_scenario"] = draft
        return report

    @staticmethod
    def _example(params: dict[str, Any], profile: Profile) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in params.items():
            if k in profile.test_customer_fields:
                out[k] = "{{test_customer.%s}}" % k
            else:
                out[k] = v
        return json_ready(out)

    def _masked_example(self, params: dict[str, Any], profile: Profile) -> dict[str, Any]:
        """Catalog examples are written to disk (and often to git): personal data is masked."""
        return self.engine.masker.mask(self._example(params, profile))

    def _draft(self, service: str, profile: Profile, order: list[Match], source: str) -> str | None:
        import yaml

        steps = []
        previous = None
        for m in order:
            if m.operation == previous:
                continue
            previous = m.operation
            body = m.exchange.request_body if isinstance(m.exchange.request_body, dict) else {}
            params = self._masked_example({k: v for k, v in body.items() if k not in profile.envelope_keys()
                                           and not (k.lower() in {f.lower() for f in profile.secret_fields}
                                                    or self.engine.masker._field_kind(k) == "secret")}, profile)
            step: dict[str, Any] = {"name": m.operation, "service": service, "operation": m.operation}
            if params:
                step["params"] = params
            step["assert"] = [{"success": True}]
            steps.append(step)
        if not steps:
            return None
        stem = re.sub(r"[^A-Za-z0-9]+", "-", Path(source).stem).strip("-").lower() or "log"
        scenario: dict[str, Any] = {"id": f"{stem}-flow", "title": f"{service}: flow made from the log {source}",
                                    "tags": ["draft", "from-log"], "source_log": source}
        if profile.default_test_customer:
            scenario["test_customer"] = profile.default_test_customer
        scenario["steps"] = steps
        header = ("# Draft scenario generated from a traffic log. Login is done by the profile, so login steps are\n"
                  "# not listed. Masked values (***) and personal data must be replaced with {{env.x}} /\n"
                  "# test_customer fields before this is used as a regression test.\n")
        return header + yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False, width=120)

    def _write_scenario(self, scenario_path: str, text: str) -> str:
        p = Path(scenario_path).expanduser()
        root = self.engine.settings.scenario_root
        if not p.is_absolute() and root:
            p = Path(root) / p
        if root and not str(p.resolve()).lower().startswith(str(Path(root).resolve()).lower()):
            raise PermissionError(f"scenario_path must be under scenario_root ({root})")
        if p.suffix.lower() not in (".yaml", ".yml"):
            raise ValueError("scenario_path must end with .yaml or .yml")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    @staticmethod
    def _next_steps(reports: list[dict[str, Any]], apply: bool) -> list[str]:
        steps = []
        if not reports:
            return ["Nothing to import for a profile service (see warnings)."]
        if not apply:
            steps.append("This was a preview. Show the summary to the user; after they approve, call again with "
                         "apply=true to write the catalog.")
        unknown = [o["operation"] for r in reports for o in r["operations"] if o.get("kind") == "unknown"]
        if unknown:
            steps.append("Ask the user to classify (read / mutating / auth): " + ", ".join(unknown[:20]))
        if any(o.get("credential_fields") for r in reports for o in r["operations"]):
            steps.append("Credential fields were found; they are not stored. They belong in the profile "
                         "(envelope / login params) as {{secret.STM_SECRET_...}}.")
        return steps
