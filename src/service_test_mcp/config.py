"""Service catalogue configuration (``services.yaml`` / ``services.json``).

Where it is read from:

1. ``STM_CONFIG`` - path to a YAML or JSON file.
2. Otherwise ``%USERPROFILE%\\.service-test-mcp\\services.yaml`` (or ``.yml`` / ``.json``) if it exists.

Secrets are never written in the file. A file refers to them by environment variable name
(``password_env``, ``token_env``, ``value_env``); in VS Code those variables are filled from ``inputs`` of
``mcp.json``, so VS Code asks once and keeps them in its secure storage.

A service whose definition is wrong does not stop the others: it is reported as broken with the reason.
Production-like environment names are refused on purpose: this tool is for test environments.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

PRODUCTION_NAMES = {"prod", "production", "prd", "canli", "canlı", "live", "prod-dr", "dr"}
AUTH_TYPES = ("none", "basic", "ntlm", "windows", "bearer", "api_key", "ws_username_token", "client_cert")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class ConfigError(ValueError):
    """Invalid or incomplete configuration."""


def home_dir() -> Path:
    value = os.environ.get("STM_HOME")
    return Path(value).expanduser() if value else Path.home() / ".service-test-mcp"


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on", "evet"):
        return True
    if text in ("0", "false", "no", "n", "off", "hayir", "hayır", ""):
        return False
    raise ConfigError(f"'{name}' must be true/false, got {value!r}")


def _number(value: Any, name: str, minimum: float = 0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{name}' must be a number, got {value!r}") from None
    if result < minimum:
        raise ConfigError(f"'{name}' must be >= {minimum}")
    return result


def _check_keys(where: str, data: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def secret_from_env(env_name: str, where: str) -> str:
    """Read a secret named by ``*_env``. The error tells exactly what to add to mcp.json."""
    value = os.environ.get(env_name)
    if value is None or value == "" or (value.startswith("${") and value.endswith("}")):
        raise ConfigError(
            f"{where}: environment variable '{env_name}' is not set. In VS Code add it to the MCP server's "
            f"\"env\" in mcp.json, e.g. \"{env_name}\": \"${{input:{env_name.lower().replace('_', '-')}}}\", plus a "
            f"matching promptString entry (\"password\": true) under \"inputs\". Outside VS Code set it in the "
            f"environment of the process that starts service-test-mcp.")
    return value


@dataclass
class AuthConfig:
    type: str = "none"
    user: str | None = None
    password_env: str | None = None
    token_env: str | None = None
    header: str = "X-API-Key"
    value_env: str | None = None
    use_digest: bool = False
    cert_file: str | None = None
    key_file: str | None = None

    KEYS = {"type", "user", "username", "password_env", "token_env", "header", "value_env", "use_digest",
            "cert_file", "key_file"}

    @classmethod
    def parse(cls, where: str, data: Any) -> "AuthConfig":
        if data is None:
            return cls()
        if isinstance(data, str):
            data = {"type": data}
        if not isinstance(data, Mapping):
            raise ConfigError(f"{where}.auth must be a mapping")
        _check_keys(f"{where}.auth", data, cls.KEYS)
        kind = str(data.get("type", "none")).strip().lower()
        kind = {"integrated": "windows", "kerberos": "windows", "negotiate": "windows", "sspi": "windows",
                "apikey": "api_key", "token": "bearer", "wsse": "ws_username_token",
                "username_token": "ws_username_token", "certificate": "client_cert"}.get(kind, kind)
        if kind not in AUTH_TYPES:
            raise ConfigError(f"{where}.auth.type must be one of {', '.join(AUTH_TYPES)}, got {kind!r}")
        auth = cls(type=kind, user=data.get("user") or data.get("username"), password_env=data.get("password_env"),
                   token_env=data.get("token_env"), header=str(data.get("header", "X-API-Key")),
                   value_env=data.get("value_env"), use_digest=_bool(data.get("use_digest", False), "use_digest"),
                   cert_file=data.get("cert_file"), key_file=data.get("key_file"))
        if kind in ("basic", "ntlm", "ws_username_token"):
            if not auth.user or not auth.password_env:
                raise ConfigError(f"{where}.auth: '{kind}' needs 'user' and 'password_env' (the password itself is "
                                  "never written in the file)")
        if kind == "bearer" and not auth.token_env:
            raise ConfigError(f"{where}.auth: 'bearer' needs 'token_env'")
        if kind == "api_key" and not auth.value_env:
            raise ConfigError(f"{where}.auth: 'api_key' needs 'value_env' (and optionally 'header')")
        if kind == "client_cert" and not auth.cert_file:
            raise ConfigError(f"{where}.auth: 'client_cert' needs 'cert_file' (PEM) and optionally 'key_file'")
        return auth

    def secret_env_names(self) -> list[str]:
        return [n for n in (self.password_env, self.token_env, self.value_env) if n]

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.user:
            out["user"] = self.user
        if self.type == "api_key":
            out["header"] = self.header
        for name in self.secret_env_names():
            out.setdefault("secret_env", []).append(name)
            out.setdefault("secret_set", []).append(bool(os.environ.get(name)))
        return out


@dataclass
class EnvironmentConfig:
    name: str
    description: str = ""
    verify_tls: bool | str = True          # true/false or path to a CA bundle (PEM)
    timeout_s: float = 30.0
    proxy: str | None = None               # e.g. http://proxy.sirket.local:8080 ; "none" disables env proxies
    allow_mutating: bool = False
    variables: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    KEYS = {"description", "verify_tls", "timeout_s", "proxy", "allow_mutating", "variables", "headers"}

    @classmethod
    def parse(cls, name: str, data: Any) -> "EnvironmentConfig":
        where = f"environments.{name}"
        if str(name).strip().lower() in PRODUCTION_NAMES:
            raise ConfigError(f"{where}: production environments are not allowed in service-test-mcp. Use it only "
                              "for test / preprod environments.")
        data = data or {}
        if not isinstance(data, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        _check_keys(where, data, cls.KEYS)
        verify = data.get("verify_tls", True)
        if not (isinstance(verify, str) and (os.sep in verify or "/" in verify or verify.endswith(".pem"))):
            verify = _bool(verify, f"{where}.verify_tls")
        variables = data.get("variables") or {}
        headers = data.get("headers") or {}
        if not isinstance(variables, Mapping) or not isinstance(headers, Mapping):
            raise ConfigError(f"{where}: 'variables' and 'headers' must be mappings")
        return cls(name=str(name), description=str(data.get("description", "")), verify_tls=verify,
                   timeout_s=_number(data.get("timeout_s", 30), f"{where}.timeout_s", 1),
                   proxy=data.get("proxy"), allow_mutating=_bool(data.get("allow_mutating", False), "allow_mutating"),
                   variables=dict(variables), headers={str(k): str(v) for k, v in headers.items()})


def _per_env(where: str, value: Any) -> dict[str, str] | str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    raise ConfigError(f"{where} must be a string or a mapping of environment -> value")


@dataclass
class ServiceConfig:
    name: str
    protocol: str
    description: str = ""
    wsdl: dict[str, str] | str | None = None
    endpoint: dict[str, str] | str | None = None
    base_url: dict[str, str] | str | None = None
    openapi: dict[str, str] | str | None = None
    auth: AuthConfig = field(default_factory=AuthConfig)
    auth_overrides: dict[str, AuthConfig] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    service_name: str | None = None        # SOAP: WSDL service to bind (default: first)
    port_name: str | None = None           # SOAP: WSDL port to bind (default: first SOAP port)
    ws_addressing: bool = False
    timeout_s: float | None = None
    read_only: bool = False
    mutating_operations: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    environments: list[str] | None = None  # restrict to these environments
    profile: str | None = None             # REST only: profile file (catalog, envelope, session, test customers)

    KEYS = {"protocol", "description", "wsdl", "endpoint", "base_url", "openapi", "auth", "auth_overrides",
            "headers", "service_name", "port_name", "ws_addressing", "timeout_s", "read_only",
            "mutating_operations", "allowed_hosts", "environments", "profile"}

    @classmethod
    def parse(cls, name: str, data: Any) -> "ServiceConfig":
        where = f"services.{name}"
        if not isinstance(data, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        _check_keys(where, data, cls.KEYS)
        protocol = str(data.get("protocol", "")).strip().lower()
        if protocol not in ("soap", "rest"):
            raise ConfigError(f"{where}.protocol must be 'soap' or 'rest'")
        svc = cls(name=str(name), protocol=protocol, description=str(data.get("description", "")),
                  wsdl=_per_env(f"{where}.wsdl", data.get("wsdl")),
                  endpoint=_per_env(f"{where}.endpoint", data.get("endpoint")),
                  base_url=_per_env(f"{where}.base_url", data.get("base_url")),
                  openapi=_per_env(f"{where}.openapi", data.get("openapi")),
                  auth=AuthConfig.parse(where, data.get("auth")),
                  headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
                  service_name=data.get("service_name"), port_name=data.get("port_name"),
                  ws_addressing=_bool(data.get("ws_addressing", False), f"{where}.ws_addressing"),
                  timeout_s=_number(data["timeout_s"], f"{where}.timeout_s", 1) if data.get("timeout_s") else None,
                  read_only=_bool(data.get("read_only", False), f"{where}.read_only"),
                  mutating_operations=[str(x) for x in data.get("mutating_operations") or []],
                  allowed_hosts=[str(x).lower() for x in data.get("allowed_hosts") or []],
                  environments=[str(x) for x in data["environments"]] if data.get("environments") else None,
                  profile=str(data["profile"]) if data.get("profile") else None)
        for env_name, auth in (data.get("auth_overrides") or {}).items():
            svc.auth_overrides[str(env_name)] = AuthConfig.parse(f"{where}.auth_overrides.{env_name}", auth)
        if protocol == "soap" and not svc.wsdl:
            raise ConfigError(f"{where}: SOAP services need 'wsdl' (URL or file path, per environment)")
        if protocol == "rest" and not svc.base_url:
            raise ConfigError(f"{where}: REST services need 'base_url' (per environment)")
        if svc.profile and protocol != "rest":
            raise ConfigError(f"{where}: 'profile' is only for REST (HTTP + JSON) services")
        return svc

    def value_for(self, attr: str, env: str) -> str | None:
        value = getattr(self, attr)
        if isinstance(value, dict):
            return value.get(env) or value.get("*") or value.get("default")
        return value

    def auth_for(self, env: str) -> AuthConfig:
        return self.auth_overrides.get(env, self.auth)

    def is_mutating(self, operation: str, method: str | None = None) -> bool:
        """SOAP: operation name; REST: 'METHOD /path'. Patterns are fnmatch-style, case-insensitive."""
        if self.read_only:
            if self.protocol == "rest":
                return (method or "GET").upper() not in SAFE_METHODS
            return True
        key = operation if self.protocol == "soap" else f"{(method or 'GET').upper()} {operation}"
        return any(fnmatch.fnmatch(key.lower(), pat.lower()) for pat in self.mutating_operations)

    def hosts_for(self, env: str) -> set[str]:
        hosts = set(self.allowed_hosts)
        for attr in ("wsdl", "endpoint", "base_url", "openapi"):
            url = self.value_for(attr, env)
            if url and "://" in url:
                host = urlsplit(url).hostname
                if host:
                    hosts.add(host.lower())
        return hosts

    def public(self, env: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "protocol": self.protocol}
        if self.description:
            out["description"] = self.description
        for attr in ("wsdl", "endpoint", "base_url", "openapi"):
            value = getattr(self, attr)
            if value:
                out[attr] = self.value_for(attr, env) if env else value
        out["auth"] = self.auth_for(env).public() if env else self.auth.public()
        if self.read_only:
            out["read_only"] = True
        if self.mutating_operations:
            out["mutating_operations"] = self.mutating_operations
        if self.environments:
            out["environments"] = self.environments
        if self.profile:
            out["profile"] = self.profile
        return out


@dataclass
class LocksConfig:
    """Test customer leases: one login per test customer at a time, across processes (VS Code windows, agents, CI).

    ``dir`` may be a shared folder (e.g. a network share) so that several machines see the same leases.
    """
    dir: str | None = None
    wait_s: float = 0.0

    @classmethod
    def parse(cls, data: Any) -> "LocksConfig":
        data = data or {}
        if not isinstance(data, Mapping):
            raise ConfigError("locks must be a mapping")
        _check_keys("locks", data, {"dir", "wait_s"})
        return cls(dir=data.get("dir"), wait_s=_number(data.get("wait_s", 0), "locks.wait_s"))


@dataclass
class MaskingConfig:
    enabled: bool = True
    keep_last: int = 4
    fields: list[str] = field(default_factory=list)       # extra field names (case-insensitive, fnmatch)
    patterns: dict[str, str] = field(default_factory=dict)  # extra name -> regex
    builtin: bool = True                                   # TCKN, IBAN, card, phone, e-mail, password fields
    unmask_fields: list[str] = field(default_factory=list)  # field names never masked by name (fnmatch)

    @classmethod
    def parse(cls, data: Any) -> "MaskingConfig":
        data = data or {}
        _check_keys("masking", data, {"enabled", "keep_last", "fields", "patterns", "builtin", "unmask_fields"})
        return cls(enabled=_bool(data.get("enabled", True), "masking.enabled"),
                   unmask_fields=[str(x) for x in data.get("unmask_fields") or []],
                   keep_last=int(_number(data.get("keep_last", 4), "masking.keep_last")),
                   fields=[str(x) for x in data.get("fields") or []],
                   patterns={str(k): str(v) for k, v in (data.get("patterns") or {}).items()},
                   builtin=_bool(data.get("builtin", True), "masking.builtin"))


@dataclass
class Settings:
    path: str | None = None
    default_environment: str | None = None
    environments: dict[str, EnvironmentConfig] = field(default_factory=dict)
    services: dict[str, ServiceConfig] = field(default_factory=dict)
    broken: dict[str, str] = field(default_factory=dict)
    masking: MaskingConfig = field(default_factory=MaskingConfig)
    blocked_hosts: list[str] = field(default_factory=list)
    max_response_chars: int = 20000
    audit_enabled: bool = True
    audit_dir: str | None = None
    scenario_root: str | None = None
    locks: LocksConfig = field(default_factory=LocksConfig)

    def environment(self, name: str | None) -> EnvironmentConfig:
        name = name or os.environ.get("STM_ENV") or self.default_environment
        if not name:
            if len(self.environments) == 1:
                return next(iter(self.environments.values()))
            raise ConfigError("No environment given and no 'default_environment' set. Available: "
                              + (", ".join(self.environments) or "(none)"))
        if str(name).strip().lower() in PRODUCTION_NAMES:
            raise ConfigError(f"Environment '{name}' looks like production; service-test-mcp refuses it.")
        if name not in self.environments:
            raise ConfigError(f"Unknown environment '{name}'. Available: {', '.join(self.environments) or '(none)'}")
        return self.environments[name]

    def service(self, name: str) -> ServiceConfig:
        if name in self.broken:
            raise ConfigError(f"Service '{name}' is misconfigured: {self.broken[name]}")
        if name not in self.services:
            lowered = {k.lower(): k for k in self.services}
            if name and name.lower() in lowered:
                return self.services[lowered[name.lower()]]
            raise ConfigError(f"Unknown service '{name}'. Configured: {', '.join(self.services) or '(none)'}. "
                              "Use list_services.")
        return self.services[name]

    def check_host(self, url: str, service: ServiceConfig, env: EnvironmentConfig) -> None:
        """Allowlist: a request may only go to hosts named in this service's configuration for this environment."""
        if "://" not in url:
            return  # local file (WSDL on disk)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ConfigError(f"Blocked URL scheme '{parts.scheme}' ({url})")
        host = (parts.hostname or "").lower()
        for pattern in self.blocked_hosts:
            if fnmatch.fnmatch(host, pattern.lower()):
                raise ConfigError(f"Host '{host}' is blocked by 'blocked_hosts' ({pattern}).")
        allowed = service.hosts_for(env.name)
        if not any(fnmatch.fnmatch(host, h) for h in allowed):
            raise ConfigError(
                f"Host '{host}' is not in the allowlist of service '{service.name}' for environment '{env.name}' "
                f"(allowed: {', '.join(sorted(allowed)) or '(none)'}). If the WSDL advertises a different address "
                f"(common with WCF behind a load balancer), set 'endpoint' for this environment or add the host to "
                f"'allowed_hosts'.")


ROOT_KEYS = {"default_environment", "environments", "services", "masking", "blocked_hosts", "max_response_chars",
             "audit", "scenario_root", "locks"}


def parse_settings(data: Mapping[str, Any], path: str | None = None) -> Settings:
    if not isinstance(data, Mapping):
        raise ConfigError("configuration root must be a mapping")
    _check_keys("configuration", data, ROOT_KEYS)
    settings = Settings(path=path)
    envs = data.get("environments") or {}
    if isinstance(envs, list):
        envs = {str(n): {} for n in envs}
    for name, env in envs.items():
        settings.environments[str(name)] = EnvironmentConfig.parse(str(name), env)
    default = data.get("default_environment")
    if default is not None:
        if str(default).strip().lower() in PRODUCTION_NAMES:
            raise ConfigError("default_environment must not be a production environment")
        if str(default) not in settings.environments:
            raise ConfigError(f"default_environment '{default}' is not defined under 'environments'")
        settings.default_environment = str(default)
    for name, svc in (data.get("services") or {}).items():
        try:
            settings.services[str(name)] = ServiceConfig.parse(str(name), svc)
        except ConfigError as exc:
            settings.broken[str(name)] = str(exc)
    settings.masking = MaskingConfig.parse(data.get("masking"))
    settings.blocked_hosts = [str(x) for x in data.get("blocked_hosts") or []]
    settings.max_response_chars = int(_number(data.get("max_response_chars", 20000), "max_response_chars", 1000))
    audit = data.get("audit") or {}
    if isinstance(audit, (bool, str)):
        audit = {"enabled": audit}
    _check_keys("audit", audit, {"enabled", "dir"})
    settings.audit_enabled = _bool(audit.get("enabled", True), "audit.enabled")
    settings.audit_dir = audit.get("dir")
    settings.scenario_root = data.get("scenario_root")
    settings.locks = LocksConfig.parse(data.get("locks"))
    return settings


def read_structured_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def config_path() -> Path | None:
    value = os.environ.get("STM_CONFIG")
    if value and not (value.startswith("${") and value.endswith("}")):
        return Path(value).expanduser()
    for name in ("services.yaml", "services.yml", "services.json"):
        candidate = home_dir() / name
        if candidate.exists():
            return candidate
    return None


def load_settings() -> Settings:
    path = config_path()
    if path is None:
        settings = Settings()
        if os.environ.get("STM_MAX_RESPONSE_CHARS"):
            settings.max_response_chars = int(os.environ["STM_MAX_RESPONSE_CHARS"])
        return settings
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path} (STM_CONFIG)")
    try:
        data = read_structured_file(path)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    settings = parse_settings(data or {}, str(path))
    if os.environ.get("STM_MAX_RESPONSE_CHARS"):
        settings.max_response_chars = int(os.environ["STM_MAX_RESPONSE_CHARS"])
    if settings.scenario_root and not os.path.isabs(settings.scenario_root):
        settings.scenario_root = str((path.parent / settings.scenario_root).resolve())
    for svc in settings.services.values():
        if svc.profile and not os.path.isabs(os.path.expanduser(svc.profile)):
            svc.profile = str((path.parent / svc.profile).resolve())
    if settings.locks.dir and not os.path.isabs(os.path.expanduser(settings.locks.dir)):
        settings.locks.dir = str((path.parent / settings.locks.dir).resolve())
    return settings
