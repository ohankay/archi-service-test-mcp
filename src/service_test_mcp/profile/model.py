"""Service profiles: how a family of HTTP + JSON operations is called, so the agent only names an operation.

A REST service in ``services.yaml`` points to a profile file with ``profile: profiles/mobile.profile.yaml``::

    name: mobile-wcf
    request:
      method: POST
      url: "{path}/{operation}"          # relative to the service base_url; {path} comes from the catalog
      envelope:                           # fields added to every request body (params win on conflict)
        MethodName: "{{operation}}"
        ExtUSessionKey: "{{session.SessionKey}}"
        DeviceId: "{{env.device_id}}"
      session_envelope:                   # added only when a test customer is logged in (may use {{test_customer.x}})
        OwnerCustomerId: "{{test_customer.CustomerId}}"
      headers: {}                         # header templates (same variables as the envelope)
    success:                              # business success rule = assertions that must all pass
      - status: { lt: 400 }
      - path: $.Success
        equals: true
    error_paths: [$.Results[*].ErrorMessage]
    session:
      ttl_s: 300                          # session lifetime on the server
      refresh_margin_s: 30                # log in again this long before it runs out
      ttl_mode: absolute                  # absolute (from login) | idle (from the last call)
      defaults: { SessionKey: "" }        # session.* values before login
      login:                              # steps; 'extract' saves session.* values
        - operation: LoginForMobileBranch
          params: { UserId: "{{test_customer.UserId}}" }
          extract: { SessionKey: $.SessionKey }
      logout: { operation: Logout }
      expired_when:                       # all must pass => the server says the session is gone
        - path: $.Results[*].ErrorCode
          in: [SESSION_EXPIRED]
      retry_mutating_on_expired: false
    test_customers:
      identity: [CustomerId]              # fields that identify a test customer (used for the login lease)
      default: bireysel
    environments:                         # everything that differs per environment
      test:
        variables: { device_id: "..." }   # merged over services.yaml environment variables ({{env.x}})
        test_customers:
          bireysel: { CustomerId: 111, UserId: "..." }
          havuz: { pool: [ { CustomerId: 1 }, { CustomerId: 2 } ] }
          yeni: { source: { operation: GetTestCustomer, params: {...}, extract: { CustomerId: $.Value.Id } } }
    noise: [InsertClientLog, "HasUnread*"]   # ignored by the traffic importer
    importer:
      test_customer_fields: [CustomerId, OwnerCustomerId, UserId]
      secret_fields: [ExtUPassword]
    catalog: mobile.catalog.yaml           # operation catalog (written by import_traffic_log, edited by people)
    hooks: hooks.py:MobileHooks           # optional Python class for logic YAML can not express

Nothing here is specific to one bank or one app: a new service family is a new profile file, not new code.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..config import ConfigError, _bool, _check_keys, _number, read_structured_file
from ..scenario.asserts import validate_assertion

OPERATION_KINDS = ("read", "mutating", "auth", "unknown")
PROFILE_KEYS = {"name", "description", "request", "success", "error_paths", "session", "test_customers", "environments",
                "noise", "importer", "catalog", "hooks"}
OPERATION_KEYS = {"path", "kind", "session", "description", "tags", "params", "request_schema", "response_schema",
                  "seen", "method"}


def _mapping(where: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    return dict(value)


def _assertions(where: str, value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list of assertions")
    for i, spec in enumerate(value):
        problems = validate_assertion(spec)
        if problems:
            raise ConfigError(f"{where}[{i}]: {'; '.join(problems)}")
    return list(value)


@dataclass
class StepSpec:
    """A login / logout / test_customer-source step: a catalog operation with params and extract."""
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    extract: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, where: str, data: Any) -> "StepSpec":
        if isinstance(data, str):
            data = {"operation": data}
        data = _mapping(where, data)
        _check_keys(where, data, {"operation", "params", "extract"})
        if not data.get("operation"):
            raise ConfigError(f"{where}: 'operation' is required")
        return cls(operation=str(data["operation"]), params=_mapping(f"{where}.params", data.get("params")),
                   extract={str(k): str(v) for k, v in _mapping(f"{where}.extract", data.get("extract")).items()})


@dataclass
class SessionSpec:
    ttl_s: float = 300.0
    refresh_margin_s: float = 30.0
    ttl_mode: str = "absolute"
    defaults: dict[str, Any] = field(default_factory=dict)
    login: list[StepSpec] = field(default_factory=list)
    logout: StepSpec | None = None
    expired_when: list[dict[str, Any]] = field(default_factory=list)
    retry_mutating_on_expired: bool = False

    @classmethod
    def parse(cls, data: Any) -> "SessionSpec":
        data = _mapping("session", data)
        _check_keys("session", data, {"ttl_s", "refresh_margin_s", "ttl_mode", "defaults", "login", "logout",
                                      "expired_when", "retry_mutating_on_expired"})
        mode = str(data.get("ttl_mode", "absolute")).lower()
        if mode not in ("absolute", "idle"):
            raise ConfigError("session.ttl_mode must be 'absolute' or 'idle'")
        login = data.get("login") or []
        if not isinstance(login, list):
            raise ConfigError("session.login must be a list of steps")
        spec = cls(ttl_s=_number(data.get("ttl_s", 300), "session.ttl_s", 1),
                   refresh_margin_s=_number(data.get("refresh_margin_s", 30), "session.refresh_margin_s"),
                   ttl_mode=mode, defaults=_mapping("session.defaults", data.get("defaults")),
                   login=[StepSpec.parse(f"session.login[{i}]", s) for i, s in enumerate(login)],
                   logout=StepSpec.parse("session.logout", data["logout"]) if data.get("logout") else None,
                   expired_when=_assertions("session.expired_when", data.get("expired_when")),
                   retry_mutating_on_expired=_bool(data.get("retry_mutating_on_expired", False),
                                                   "session.retry_mutating_on_expired"))
        if spec.refresh_margin_s >= spec.ttl_s:
            raise ConfigError("session.refresh_margin_s must be smaller than session.ttl_s")
        return spec


@dataclass
class CustomerSpec:
    """One named test customer: fixed values, a pool of interchangeable customers, or an API that hands one out."""
    name: str
    pool: list[dict[str, Any]] = field(default_factory=list)
    source: StepSpec | None = None
    description: str = ""

    @classmethod
    def parse(cls, where: str, name: str, data: Any) -> "CustomerSpec":
        data = _mapping(where, data)
        description = str(data.pop("description", "") or "")
        if "source" in data:
            if set(data) != {"source"}:
                raise ConfigError(f"{where}: a 'source' test_customer has no other fields (besides description)")
            return cls(name=name, source=StepSpec.parse(f"{where}.source", data["source"]), description=description)
        if "pool" in data:
            if set(data) != {"pool"}:
                raise ConfigError(f"{where}: a 'pool' test_customer has no other fields (besides description)")
            pool = data["pool"]
            if not isinstance(pool, list) or not pool or not all(isinstance(p, Mapping) for p in pool):
                raise ConfigError(f"{where}.pool must be a non-empty list of mappings")
            return cls(name=name, pool=[dict(p) for p in pool], description=description)
        if not data:
            raise ConfigError(f"{where}: give the test customer's fields, a 'pool' or a 'source'")
        return cls(name=name, pool=[data], description=description)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description:
            out["description"] = self.description
        if self.source:
            out["type"] = "source"
            out["source_operation"] = self.source.operation
        else:
            out["type"] = "pool" if len(self.pool) > 1 else "fixed"
            out["size"] = len(self.pool)
            out["fields"] = sorted({k for p in self.pool for k in p})
        return out


@dataclass
class ProfileEnvironment:
    variables: dict[str, Any] = field(default_factory=dict)
    test_customers: dict[str, CustomerSpec] = field(default_factory=dict)
    default_customer: str | None = None

    @classmethod
    def parse(cls, name: str, data: Any) -> "ProfileEnvironment":
        where = f"environments.{name}"
        data = _mapping(where, data)
        _check_keys(where, data, {"variables", "test_customers"})
        test_customers = {str(k): CustomerSpec.parse(f"{where}.test_customers.{k}", str(k), v)
                    for k, v in _mapping(f"{where}.test_customers", data.get("test_customers")).items()}
        return cls(variables=_mapping(f"{where}.variables", data.get("variables")), test_customers=test_customers)


@dataclass
class Operation:
    name: str
    path: str = ""
    kind: str = "unknown"
    session: bool = True
    method: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    seen: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, name: str, data: Any) -> "Operation":
        where = f"operations.{name}"
        data = _mapping(where, data)
        _check_keys(where, data, OPERATION_KEYS)
        kind = str(data.get("kind", "unknown")).lower()
        if kind not in OPERATION_KINDS:
            raise ConfigError(f"{where}.kind must be one of {', '.join(OPERATION_KINDS)}")
        tags = data.get("tags") or []
        return cls(name=name, path=str(data.get("path") or "").strip("/"), kind=kind,
                   session=_bool(data.get("session", True), f"{where}.session"),
                   method=str(data["method"]).upper() if data.get("method") else None,
                   description=str(data.get("description") or ""), tags=[str(t) for t in tags],
                   params=_mapping(f"{where}.params", data.get("params")),
                   request_schema=data.get("request_schema"), response_schema=data.get("response_schema"),
                   seen=_mapping(f"{where}.seen", data.get("seen")))

    @property
    def mutating(self) -> bool:
        """'unknown' counts as mutating: an operation nobody classified is not assumed harmless."""
        return self.kind in ("mutating", "unknown")

    def to_data(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "kind": self.kind, "session": self.session}
        if self.method:
            out["method"] = self.method
        if self.description:
            out["description"] = self.description
        if self.tags:
            out["tags"] = self.tags
        if self.params:
            out["params"] = self.params
        if self.request_schema:
            out["request_schema"] = self.request_schema
        if self.response_schema:
            out["response_schema"] = self.response_schema
        if self.seen:
            out["seen"] = self.seen
        return out

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"operation": self.name, "kind": self.kind, "session": self.session}
        if self.description:
            out["description"] = self.description
        if self.tags:
            out["tags"] = self.tags
        if self.params:
            out["params"] = sorted(self.params)
        return out


@dataclass
class Catalog:
    path: Path | None
    operations: dict[str, Operation] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> "Catalog":
        if path is None or not path.exists():
            return cls(path=path)
        data = read_structured_file(path) or {}
        data = _mapping(f"catalog {path.name}", data)
        _check_keys(f"catalog {path.name}", data, {"operations"})
        ops = {str(k): Operation.parse(str(k), v) for k, v in _mapping("operations", data.get("operations")).items()}
        return cls(path=path, operations=ops)

    def get(self, name: str) -> Operation | None:
        if name in self.operations:
            return self.operations[name]
        lowered = {k.lower(): v for k, v in self.operations.items()}
        return lowered.get(str(name).lower())

    def search(self, text: str | None = None, tag: str | None = None, kind: str | None = None) -> list[Operation]:
        out = []
        for op in self.operations.values():
            if text and text.lower() not in (op.name + " " + op.description + " " + op.path).lower() and \
                    not fnmatch.fnmatch(op.name.lower(), text.lower()):
                continue
            if tag and tag.lower() not in {t.lower() for t in op.tags}:
                continue
            if kind and op.kind != kind:
                continue
            out.append(op)
        return sorted(out, key=lambda o: o.name)

    def save(self) -> None:
        if self.path is None:
            raise ConfigError("profile has no 'catalog' file")
        import yaml

        data = {"operations": {name: self.operations[name].to_data() for name in sorted(self.operations)}}
        text = ("# Operation catalog. Written by import_traffic_log; descriptions, kind and tags are kept when it\n"
                "# updates an operation, so they can be edited by hand.\n")
        text += yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class Profile:
    path: Path
    name: str
    description: str = ""
    method: str = "POST"
    url: str = "{path}/{operation}"
    envelope: dict[str, Any] = field(default_factory=dict)
    session_envelope: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)
    success: list[dict[str, Any]] = field(default_factory=list)
    error_paths: list[str] = field(default_factory=list)
    session: SessionSpec = field(default_factory=SessionSpec)
    identity: list[str] = field(default_factory=list)
    default_test_customer: str | None = None
    environments: dict[str, ProfileEnvironment] = field(default_factory=dict)
    noise: list[str] = field(default_factory=list)
    test_customer_fields: list[str] = field(default_factory=list)
    secret_fields: list[str] = field(default_factory=list)
    catalog_path: Path | None = None
    hooks: str | None = None
    catalog: Catalog = field(default_factory=lambda: Catalog(path=None))
    # set when the profile is compiled from definitions (definitions/): rules in the check language, dotted paths,
    # and test customers given just by their number (adhoc_field = the variable that holds it)
    success_lines: list[str] = field(default_factory=list)
    expired_lines: list[str] = field(default_factory=list)
    dotted_paths: bool = False
    adhoc_field: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        p = Path(path).expanduser()
        if not p.exists():
            raise ConfigError(f"Profile file not found: {p}")
        try:
            data = read_structured_file(p) or {}
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"Cannot read profile {p}: {exc}") from None
        return cls.parse(data, p)

    @classmethod
    def parse(cls, data: Any, path: Path) -> "Profile":
        data = _mapping(f"profile {path.name}", data)
        _check_keys(f"profile {path.name}", data, PROFILE_KEYS)
        request = _mapping("request", data.get("request"))
        _check_keys("request", request, {"method", "url", "envelope", "session_envelope", "headers"})
        test_customers = _mapping("test_customers", data.get("test_customers"))
        _check_keys("test_customers", test_customers, {"identity", "default"})
        importer = _mapping("importer", data.get("importer"))
        _check_keys("importer", importer, {"test_customer_fields", "secret_fields"})
        url = str(request.get("url", "{path}/{operation}"))
        if "{operation}" not in url:
            raise ConfigError("request.url must contain {operation}")
        error_paths = data.get("error_paths") or []
        if not isinstance(error_paths, list):
            raise ConfigError("error_paths must be a list of JSONPath expressions")
        catalog_path = None
        if data.get("catalog"):
            catalog_path = Path(str(data["catalog"])).expanduser()
            if not catalog_path.is_absolute():
                catalog_path = (path.parent / catalog_path).resolve()
        profile = cls(path=path, name=str(data.get("name") or path.stem),
                      description=str(data.get("description") or ""),
                      method=str(request.get("method", "POST")).upper(), url=url,
                      envelope=_mapping("request.envelope", request.get("envelope")),
                      session_envelope=_mapping("request.session_envelope", request.get("session_envelope")),
                      headers=_mapping("request.headers", request.get("headers")),
                      success=_assertions("success", data.get("success")), error_paths=[str(x) for x in error_paths],
                      session=SessionSpec.parse(data.get("session")),
                      identity=[str(x) for x in test_customers.get("identity") or []],
                      default_test_customer=str(test_customers["default"]) if test_customers.get("default") else None,
                      environments={str(k): ProfileEnvironment.parse(str(k), v)
                                    for k, v in _mapping("environments", data.get("environments")).items()},
                      noise=[str(x) for x in data.get("noise") or []],
                      test_customer_fields=[str(x) for x in importer.get("test_customer_fields") or []],
                      secret_fields=[str(x) for x in importer.get("secret_fields") or []],
                      catalog_path=catalog_path, hooks=str(data["hooks"]) if data.get("hooks") else None)
        profile.catalog = Catalog.load(catalog_path)
        return profile

    def environment(self, name: str) -> ProfileEnvironment:
        return self.environments.get(name) or ProfileEnvironment()

    def envelope_keys(self) -> set[str]:
        return set(self.envelope) | set(self.session_envelope)

    def is_noise(self, operation: str) -> bool:
        return any(fnmatch.fnmatch(operation.lower(), pat.lower()) for pat in self.noise)

    def public(self, env: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"profile": self.name, "operations": len(self.catalog.operations),
                               "login_steps": [s.operation for s in self.session.login],
                               "session_ttl_s": self.session.ttl_s}
        if self.description:
            out["description"] = self.description
        if self.default_test_customer:
            out["default_test_customer"] = self.default_test_customer
        if env:
            out["test_customers"] = [p.public() for p in self.environment(env).test_customers.values()]
        return out
