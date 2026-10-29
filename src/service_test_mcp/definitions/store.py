"""The definitions folder: load, find by name, validate, save.

    definitions/
      systems/      mobile-banking.yaml   how to reach a system (environments, login, envelope)
      operations/   account-list.yaml     one operation with friendly inputs
      approaches/   boundary-value.yaml   a test design technique
      tests/        ...yaml               runnable tests

Folder: ``STM_DEFINITIONS``, otherwise ``%USERPROFILE%\\.service-test-mcp\\definitions``. Every save is validated
first (schema + meaning: referenced operations exist, checks parse ...). A save carries the version it was based on,
so the agent and a UI editing the same file do not overwrite each other.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import env_setting, home_dir
from .checks import CheckError, parse
from .schemas import EXAMPLE_KEYS, FOLDERS, KINDS, SCHEMAS

_TEMPLATE = re.compile(r"\{\{[^{}]+\}\}")
PRODUCTION_WORDS = ("prod", "production", "prd", "live", "canli", "canlı")


class DefinitionError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def definitions_folder() -> Path:
    value = env_setting("STM_DEFINITIONS")
    return Path(value).expanduser() if value else home_dir() / "definitions"


def slug(name: str) -> str:
    text = str(name).replace("ı", "i").replace("İ", "i")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "definition"


def version_of(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class Record:
    kind: str
    data: dict[str, Any]
    path: Path | None
    version: str = ""

    @property
    def name(self) -> str:
        return str(self.data.get("name", ""))


def _check_kind(kind: str) -> None:
    if kind not in SCHEMAS:
        raise DefinitionError([f"unknown definition kind '{kind}'; use one of: {', '.join(KINDS)}"])


@dataclass
class Store:
    root: Path
    records: dict[str, dict[str, Record]] = field(default_factory=dict)   # kind -> slug -> Record
    load_errors: dict[str, list[str]] = field(default_factory=dict)       # file -> problems

    @classmethod
    def load(cls, root: Path | None = None) -> "Store":
        import yaml

        store = cls(root=(root or definitions_folder()))
        for kind, folder_name in FOLDERS.items():
            store.records[kind] = {}
            folder = store.root / folder_name
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.y*ml")):
                if path.name.startswith((".", "~")):
                    continue
                text = path.read_text(encoding="utf-8-sig")
                try:
                    data = yaml.safe_load(text) or {}
                except Exception as exc:  # noqa: BLE001
                    store.load_errors[str(path)] = [f"cannot read YAML: {exc}"]
                    continue
                if not isinstance(data, dict):
                    store.load_errors[str(path)] = ["the file must be a mapping (key: value)"]
                    continue
                data = forgive(kind, data)
                store.records[kind][slug(data.get("name") or path.stem)] = Record(kind, data, path, version_of(text))
        for kind in FOLDERS:
            for record in list(store.records[kind].values()):
                problems = store.validate(kind, record.data)
                if problems:
                    store.load_errors[str(record.path)] = problems
        return store

    # -- lookup -------------------------------------------------------------------------------------------------
    def all(self, kind: str) -> list[Record]:
        return list(self.records.get(kind, {}).values())

    def find(self, kind: str, name: str) -> Record | None:
        items = self.records.get(kind, {})
        key = slug(name)
        if key in items:
            return items[key]
        for record in items.values():
            if kind == "operation" and str(record.data.get("method", "")).lower() == str(name).lower():
                return record
            if kind == "approach" and key in {slug(x) for x in record.data.get("aliases") or []}:
                return record
        return None

    def get(self, kind: str, name: str) -> Record:
        _check_kind(kind)
        record = self.find(kind, name)
        if record is None:
            names = ", ".join(r.name for r in self.all(kind)) or "(none)"
            hint = {"operation": " Use that screen of the app, give the log to import_log and it will be defined.",
                    "test": " It can be made with generate_tests or written with save_definition."}.get(kind, "")
            raise DefinitionError([f"{kind.capitalize()} '{name}' is not defined. Defined: {names}.{hint}"])
        return record

    def system_of(self, operation: dict[str, Any]) -> Record:
        if operation.get("system"):
            return self.get("system", operation["system"])
        systems = self.all("system")
        if len(systems) == 1:
            return systems[0]
        raise DefinitionError([f"Operation '{operation.get('name')}': which system? Write 'system:' "
                               f"({', '.join(s.name for s in systems) or 'no system defined'})"])

    # -- validation ---------------------------------------------------------------------------------------------
    def validate(self, kind: str, data: dict[str, Any]) -> list[str]:
        import jsonschema

        if kind not in SCHEMAS:
            return [f"unknown definition kind '{kind}'; use one of: {', '.join(KINDS)}"]
        data = forgive(kind, data)
        problems = []
        validator = jsonschema.Draft202012Validator(SCHEMAS[kind])
        for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
            where = ".".join(str(p) for p in err.absolute_path) or "(root)"
            problems.append(f"{where}: {_explain(err)}")
        if problems:
            return problems
        return getattr(self, f"_meaning_{kind}")(data)

    @staticmethod
    def _checks(where: str, lines: list[str] | None) -> list[str]:
        out = []
        for line in lines or []:
            try:
                parse(_TEMPLATE.sub("1", str(line)))
            except CheckError as exc:
                out.append(f"{where}: {exc}")
        return out

    def _meaning_system(self, v: dict[str, Any]) -> list[str]:
        out = self._checks("success", v.get("success"))
        out += self._checks("session.expired_when", (v.get("session") or {}).get("expired_when"))
        if any(str(x).strip().lower() in ("success", "failure") for x in v.get("success") or []):
            out.append("success: 'success' cannot define itself; write the response field (e.g. 'Success = true')")
        for i, step in enumerate((v.get("session") or {}).get("login") or []):
            if not step.get("path"):
                out.append(f"session.login[{i}] ({step.get('method')}): 'path' is required (e.g. X/XService.svc)")
        for name, env in (v.get("environments") or {}).items():
            if str(name).lower() in PRODUCTION_WORDS:
                out.append(f"environments.{name}: production environments cannot be defined")
            if "://" not in str(env.get("address", "")):
                out.append(f"environments.{name}.address: must be a full address starting with http(s)://")
        return out

    def _meaning_operation(self, v: dict[str, Any]) -> list[str]:
        out = self._checks("checks", v.get("checks"))
        systems = self.all("system")
        if v.get("system") and not self.find("system", v["system"]):
            out.append(f"system: '{v['system']}' is not defined")
        elif not v.get("system") and len(systems) > 1:
            out.append(f"system: there are several systems, which one? ({', '.join(s.name for s in systems)})")
        inputs = v.get("inputs") or {}
        for name, i in inputs.items():
            if i.get("type") == "list" and not i.get("options"):
                out.append(f"inputs.{name}: type 'list' needs 'options'")
            if i.get("type") == "date" and not i.get("format"):
                out.append(f"inputs.{name}: a date needs a 'format' (e.g. DD.MM.YYYY)")
        date_range = v.get("date_range")
        if date_range:
            for k in ("start", "end"):
                if date_range[k] not in inputs:
                    out.append(f"date_range.{k}: '{date_range[k]}' is not an input")
        return out

    def _operation_inputs(self, where: str, operation: str, given: dict[str, Any] | None) -> list[str]:
        record = self.find("operation", operation)
        if record is None:
            return [f"{where}: operation '{operation}' is not defined (import_log can define it)"]
        known = set(record.data.get("inputs") or {})
        return [f"{where}: operation '{record.name}' has no input '{k}' (inputs: {', '.join(sorted(known)) or '-'})"
                for k in (given or {}) if k not in known]

    def _meaning_test(self, v: dict[str, Any]) -> list[str]:
        out = self._checks("checks", v.get("checks"))
        for i, step in enumerate(v.get("steps") or []):
            where = f"steps[{i}]"
            out += self._operation_inputs(where, step["operation"], step.get("input"))
            out += self._checks(f"{where}.checks", step.get("checks"))
        if v.get("examples"):
            out += self._operation_inputs("common_input", v["operation"], v.get("common_input"))
            for i, row in enumerate(v["examples"]):
                inputs = {k: x for k, x in row.items() if k not in EXAMPLE_KEYS}
                out += self._operation_inputs(f"examples[{i}] ({row.get('case')})", v["operation"], inputs)
                out += self._checks(f"examples[{i}].checks", row.get("checks"))
        if v.get("approach") and not self.find("approach", v["approach"]):
            out.append(f"approach: '{v['approach']}' is not defined")
        return out

    @staticmethod
    def _meaning_approach(v: dict[str, Any]) -> list[str]:
        if v.get("generator") == "agent" and not v.get("rules"):
            return ["rules: the 'agent' generator needs the rules to write the tests by"]
        return []

    # -- save -----------------------------------------------------------------------------------------------------
    def save(self, kind: str, data: dict[str, Any], base_version: str | None = None,
             default_status: str = "draft") -> Record:
        import yaml

        _check_kind(kind)
        data = forgive(kind, dict(data))
        data.setdefault("status", default_status)
        problems = self.validate(kind, data)
        if problems:
            raise DefinitionError(problems)
        name = str(data["name"])
        existing = self.find(kind, name)
        if existing is not None and existing.path is not None and existing.path.exists():
            current = version_of(existing.path.read_text(encoding="utf-8-sig"))
            if base_version is None:
                raise DefinitionError([f"'{name}' already exists (version {current}). To update it pass the version "
                                       "you read as 'version'; for a new definition use another name."])
            if base_version != current:
                raise DefinitionError([f"'{name}' changed after you read it (now version {current}). Read it again "
                                       "and make the change on that."])
            path = existing.path
        else:
            path = self.root / FOLDERS[kind] / f"{slug(name)}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        record = Record(kind, data, path, version_of(text))
        self.records.setdefault(kind, {})[slug(name)] = record
        return record


def _line(value: Any) -> Any:
    """A check written as 'for each X: A <= B' is read by YAML as {'for each X': 'A <= B'}; turn it back."""
    if isinstance(value, dict) and len(value) == 1:
        k, v = next(iter(value.items()))
        return f"{k}: {v}"
    return value


def forgive(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Forgive the usual YAML slips of hand-written definitions (on a copy)."""
    import copy

    v = copy.deepcopy(data)

    def lines(holder: Any, key: str) -> None:
        if isinstance(holder, dict) and isinstance(holder.get(key), list):
            holder[key] = [_line(x) for x in holder[key]]

    if kind == "system":
        lines(v, "success")
        lines(v.get("session"), "expired_when")
        customer = v.get("customer")
        if isinstance(customer, dict) and customer.get("field") is False:      # field: no -> YAML false
            customer["field"] = "no"
    elif kind == "operation":
        lines(v, "checks")
    elif kind == "test":
        lines(v, "checks")
        for step in v.get("steps") or []:
            lines(step, "checks")
        for row in v.get("examples") or []:
            lines(row, "checks")
    return v


def _explain(err: Any) -> str:
    v = err.validator
    if v == "required":
        return f"required field missing: {err.message.split(' ')[0]}"
    if v == "additionalProperties":
        extra = err.message.split("(")[-1].rstrip(")") if "(" in err.message else err.message
        return f"unknown field: {extra}"
    if v == "enum":
        return f"allowed values: {', '.join(map(str, err.validator_value))}"
    if v == "type":
        return f"must be of type {err.validator_value}"
    if v == "oneOf":
        return "a test has either 'steps' or 'operation' + 'examples' (table test), not both"
    if v == "minItems":
        return "needs at least one item"
    return err.message
