"""Definitions -> engine: a ``system`` becomes a REST service with a profile, its ``operation`` files its catalog.

Templates in definitions:

    {{customer.no}}  -> {{test_customer.no}}     the customer number given for the run
    {{session.X}}                                values taken from login responses
    {{env.x}}                                    variables of the environment
    {{secret.x}}                                 STM_SECRET_X or the local secrets.yaml
    {{method}}       -> {{operation}}            the technical name of the operation being called
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..config import EnvironmentConfig, ServiceConfig
from ..profile.model import Catalog, Operation, Profile, ProfileEnvironment, SessionSpec, StepSpec
from .store import Record, Store

KINDS = ("read", "mutating", "auth", "unknown")


def template(value: Any) -> Any:
    """Definition template names -> engine names, recursively."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            inner = m.group(1).strip()
            if inner == "method":
                return "{{operation}}"
            if inner.startswith("customer."):
                inner = "test_customer." + inner[len("customer."):]
            return "{{" + inner + "}}"
        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", repl, value)
    if isinstance(value, dict):
        return {k: template(v) for k, v in value.items()}
    if isinstance(value, list):
        return [template(v) for v in value]
    return value


def _step(step: dict[str, Any]) -> StepSpec:
    return StepSpec(operation=str(step["method"]), params=template(step.get("input") or {}),
                    extract={str(k): str(v) for k, v in (step.get("extract") or {}).items()})


def to_operation(op: dict[str, Any]) -> Operation:
    kind = str(op.get("kind") or "unknown")
    return Operation(name=str(op["method"]), path=str(op.get("path") or "").strip("/"),
                     kind=kind if kind in KINDS else "unknown", session=bool(op.get("session", True)),
                     method=str(op["http_method"]).upper() if op.get("http_method") else None,
                     description=str(op.get("name") or ""))


def compile_system(store: Store, record: Record) -> tuple[ServiceConfig, dict[str, EnvironmentConfig], Profile]:
    v = record.data
    request = v.get("request") or {}
    session_def = v.get("session") or {}
    customer_field = str((v.get("customer") or {}).get("field") or "no")
    url = str(request.get("url_pattern") or "{path}/{method}").replace("{method}", "{operation}")
    session = SessionSpec(
        ttl_s=float(session_def.get("ttl_s", 300)), refresh_margin_s=float(session_def.get("refresh_margin_s", 30)),
        ttl_mode="idle" if session_def.get("ttl_mode") == "idle" else "absolute",
        defaults=dict(session_def.get("defaults") or {}),
        login=[_step(a) for a in session_def.get("login") or []],
        logout=_step(session_def["logout"]) if session_def.get("logout") else None,
        retry_mutating_on_expired=bool(session_def.get("retry_mutating", False)))
    shared = dict(v.get("variables") or {})
    environments = {}
    envs: dict[str, EnvironmentConfig] = {}
    base_urls = {}
    for name, env in (v.get("environments") or {}).items():
        environments[name] = ProfileEnvironment(variables=template({**shared, **(env.get("variables") or {})}),
                                                default_customer=str(env["default_customer"])
                                                if env.get("default_customer") is not None else None)
        proxy = env.get("proxy")
        if isinstance(proxy, str) and proxy.strip().lower() in ("none", "direct", "off"):
            proxy = "none"
        elif isinstance(proxy, str) and proxy.strip().lower() in ("auto", "windows"):
            proxy = "auto"
        envs[name] = EnvironmentConfig(name=name, description=str(env.get("description") or ""),
                                       allow_mutating=bool(env.get("allow_mutating", True)),
                                       timeout_s=float(env.get("timeout_s") or 30),
                                       proxy=str(proxy) if proxy else None,
                                       verify_tls=_tls(env.get("verify_tls", True)))
        base_urls[name] = str(env["address"]).rstrip("/")
    log = v.get("log_fields") or {}
    profile = Profile(
        path=record.path or Path(f"{record.name}.yaml"), name=record.name, description=str(v.get("description") or ""),
        method=str(request.get("method") or "POST").upper(), url=url,
        envelope=template(request.get("envelope") or {}),
        session_envelope=template(request.get("session_envelope") or {}),
        headers=template(request.get("headers") or {}), error_paths=[str(x) for x in v.get("error_fields") or []],
        session=session, identity=[customer_field], environments=environments,
        noise=[str(x) for x in v.get("noise") or []],
        test_customer_fields=[str(x) for x in log.get("customer") or []],
        secret_fields=[str(x) for x in log.get("secret") or []] + [str(s["field"]) for s in
                                                                   (v.get("secret_values") or {}).values()],
        success_lines=[str(x) for x in v.get("success") or []],
        expired_lines=[str(x) for x in session_def.get("expired_when") or []],
        dotted_paths=True, adhoc_field=customer_field, hooks=str(v["hook"]) if v.get("hook") else None)
    ops: dict[str, Operation] = {}
    for op_record in store.all("operation"):
        try:
            if store.system_of(op_record.data).name != record.name:
                continue
        except Exception:  # noqa: BLE001 - reported by validation
            continue
        op = to_operation(op_record.data)
        ops[op.name] = op
    for step in (session_def.get("login") or []) + ([session_def["logout"]] if session_def.get("logout") else []):
        if step["method"] not in ops:
            ops[step["method"]] = Operation(name=step["method"], path=str(step.get("path") or "").strip("/"),
                                            kind="auth", session=step is session_def.get("logout"))
    profile.catalog = Catalog(path=None, operations=ops)
    service = ServiceConfig(name=record.name, protocol="rest", description=str(v.get("description") or ""),
                            base_url=base_urls)
    return service, envs, profile


def _tls(value: Any) -> bool | str:
    """true / false, or the path of a CA bundle (.pem)."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("false", "no", "0"):
            return False
        if low in ("true", "yes", "1", ""):
            return True
        return os.path.expanduser(os.path.expandvars(value.strip()))
    return bool(value)
