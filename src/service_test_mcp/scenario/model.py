"""Scenario files (YAML or JSON).

A file holds one scenario, or several under ``scenarios:``::

    id: hesap-bakiye-01
    title: Gecerli musteri icin bakiye sorgusu
    tags: [hesap, smoke]
    environment: test            # optional, default from services.yaml
    variables:
      musteriNo: "{{env.test_musteri_no}}"
    steps:
      - name: Bakiye sorgula
        service: HesapServisi    # SOAP service -> operation + params
        operation: GetBakiye
        params: { musteriNo: "{{musteriNo}}" }
        assert:
          - fault: none
          - path: $.Bakiye
            type: decimal
        extract:
          hesapNo: $.HesapNo
      - name: Hesap detayi
        service: HesapApi        # REST service -> method + path (+ query, headers, body)
        method: GET
        path: /hesaplar/{{hesapNo}}
        assert:
          - status: 200

Profile services (``profile:`` in services.yaml) are called by operation name; login, session, envelope and
headers come from the profile. ``persona`` picks the test customer (scenario-wide or per step)::

    persona: bireysel_standart
    steps:
      - name: Dashboard
        service: MobilBankacilik
        operation: GetDashBoardMobileInformation
        params: { MainResourceCode: DASHBOARD }
        assert:
          - success: true

The protocol of a step comes from the service definition; ``protocol:`` in a step is optional and only checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .asserts import validate_assertion
from .template import placeholders

SCENARIO_KEYS = {"id", "title", "description", "tags", "environment", "variables", "steps", "owner", "priority",
                 "persona", "source_log"}
STEP_KEYS = {"name", "service", "protocol", "operation", "params", "input", "method", "path", "query", "headers",
             "body", "body_text", "form", "assert", "assertions", "extract", "continue_on_failure", "skip",
             "description", "persona", "fresh_session"}
SOAP_ONLY = {"operation", "params", "input"}
REST_ONLY = {"method", "path", "query", "body", "body_text", "form"}
PROFILE_ONLY = {"persona", "fresh_session"}
PROTOCOLS = ("soap", "rest", "profile")


class ScenarioError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Step:
    name: str
    service: str
    raw: dict[str, Any]
    assertions: list[Any] = field(default_factory=list)
    extract: dict[str, str] = field(default_factory=dict)
    continue_on_failure: bool = False
    skip: bool = False


@dataclass
class Scenario:
    id: str
    title: str
    steps: list[Step]
    tags: list[str] = field(default_factory=list)
    environment: str | None = None
    persona: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    source: str | None = None
    base_dir: Path | None = None


def _step(index: int, data: Any, protocols: dict[str, str] | None, problems: list[str]) -> Step | None:
    where = f"steps[{index}]"
    if not isinstance(data, dict):
        problems.append(f"{where} must be a mapping")
        return None
    unknown = set(data) - STEP_KEYS
    if unknown:
        problems.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    service = data.get("service")
    if not service:
        problems.append(f"{where}: 'service' is required")
        return None
    name = str(data.get("name") or f"step {index + 1}")
    where = f"steps[{index}] '{name}'"
    protocol = None
    if protocols is not None:
        protocol = protocols.get(str(service)) or protocols.get(str(service).lower())
        if protocol is None:
            problems.append(f"{where}: unknown service '{service}' (configured: {', '.join(sorted(protocols)) or 'none'})")
    declared = data.get("protocol")
    if declared and str(declared).lower() not in PROTOCOLS:
        problems.append(f"{where}: protocol must be soap, rest or profile")
    if declared and protocol and str(declared).lower() != protocol:
        problems.append(f"{where}: protocol '{declared}' does not match service '{service}' ({protocol})")
    protocol = protocol or (str(declared).lower() if declared else None)
    if protocol is None:
        if "operation" in data:
            protocol = "profile" if set(data) & PROFILE_ONLY else "soap"
        else:
            protocol = "rest" if "method" in data or "path" in data else None
    if protocol in ("soap", "profile"):
        kind = "SOAP" if protocol == "soap" else "profile"
        if not data.get("operation"):
            problems.append(f"{where}: {kind} step needs 'operation'")
        wrong = (set(data) & REST_ONLY) | (set(data) & PROFILE_ONLY if protocol == "soap" else set())
        if wrong:
            problems.append(f"{where}: {', '.join(sorted(wrong))} not valid for a {kind} step (use params)")
        if "params" in data and "input" in data:
            problems.append(f"{where}: use either 'params' or 'input', not both")
    elif protocol == "rest":
        if not data.get("path") and data.get("path") != "":
            problems.append(f"{where}: REST step needs 'path'")
        wrong = (set(data) & SOAP_ONLY) | (set(data) & PROFILE_ONLY)
        if wrong:
            problems.append(f"{where}: {', '.join(sorted(wrong))} not valid for a REST step")
        if sum(1 for k in ("body", "body_text", "form") if k in data) > 1:
            problems.append(f"{where}: use only one of body, body_text, form")
    assertions = data.get("assert", data.get("assertions")) or []
    if not isinstance(assertions, list):
        problems.append(f"{where}: 'assert' must be a list")
        assertions = []
    for i, spec in enumerate(assertions):
        for p in validate_assertion(spec):
            problems.append(f"{where} assert[{i}]: {p}")
    extract = data.get("extract") or {}
    if not isinstance(extract, dict):
        problems.append(f"{where}: 'extract' must be a mapping of variable -> JSONPath")
        extract = {}
    else:
        from jsonpath_ng.ext import parse

        for var, path in extract.items():
            try:
                parse(str(path))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{where} extract.{var}: invalid JSONPath {path!r}: {exc}")
    return Step(name=name, service=str(service), raw=data, assertions=assertions,
                extract={str(k): str(v) for k, v in extract.items()},
                continue_on_failure=bool(data.get("continue_on_failure", False)), skip=bool(data.get("skip", False)))


def parse_scenario(data: Any, protocols: dict[str, str] | None = None, source: str | None = None,
                   base_dir: Path | None = None) -> Scenario:
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ScenarioError(["scenario must be a mapping"])
    unknown = set(data) - SCENARIO_KEYS
    if unknown:
        problems.append(f"unknown key(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(SCENARIO_KEYS))}")
    sid = str(data.get("id") or (Path(source).stem if source else "")).strip()
    if not sid:
        problems.append("'id' is required")
    steps_data = data.get("steps")
    if not isinstance(steps_data, list) or not steps_data:
        problems.append("'steps' must be a non-empty list")
        steps_data = []
    variables = data.get("variables") or {}
    if not isinstance(variables, dict):
        problems.append("'variables' must be a mapping")
        variables = {}
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    steps = [s for i, d in enumerate(steps_data) if (s := _step(i, d, protocols, problems)) is not None]
    # variables used before they are defined
    known = set(variables)
    for step in steps:
        for expr in placeholders(step.raw):
            root = expr.split(".")[0].split("[")[0]
            if expr.startswith(("$", "env.", "secret.", "persona.", "session.")) or root in known:
                continue
            problems.append(f"step '{step.name}': variable '{{{{{expr}}}}}' is not defined before this step "
                            "(variables / earlier extract)")
        known |= set(step.extract)
    if problems:
        raise ScenarioError([f"[{sid or source or 'scenario'}] {p}" for p in problems])
    return Scenario(id=sid, title=str(data.get("title") or sid), steps=steps, tags=[str(t) for t in tags],
                    environment=data.get("environment"), variables=variables,
                    persona=str(data["persona"]) if data.get("persona") else None,
                    description=str(data.get("description") or ""), source=source, base_dir=base_dir)


def load_documents(data: Any) -> list[Any]:
    if isinstance(data, dict) and "scenarios" in data and "steps" not in data:
        items = data["scenarios"]
        return items if isinstance(items, list) else [items]
    if isinstance(data, list):
        return data
    return [data]


def parse_text(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def load_file(path: Path) -> Any:
    from ..config import read_structured_file

    return read_structured_file(path)


def scenario_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in (".yaml", ".yml", ".json") and p.is_file()
                  and not p.name.startswith(("_", ".")))
