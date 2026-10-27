"""SOAP / WCF adapter (zeep).

Supported: SOAP 1.1 and 1.2 over HTTP(S) - WCF ``basicHttpBinding``, ``wsHttpBinding`` without message security,
``webHttpBinding`` SOAP endpoints; Transport security (HTTPS), Windows/NTLM/basic credentials, WS-Security
UsernameToken, optional WS-Addressing. Not supported (WCF-only stacks): ``netTcpBinding``, ``netNamedPipeBinding``,
message security with SecureConversation - ask the service owner for a ``basicHttpBinding`` endpoint on test.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import ConfigError, EnvironmentConfig, ServiceConfig, Settings, secret_from_env
from .normalize import normalize
from .transport import build_session

INTEGER_TYPES = {"int", "integer", "long", "short", "byte", "unsignedint", "unsignedlong", "unsignedshort",
                 "unsignedbyte", "nonnegativeinteger", "positiveinteger", "negativeinteger", "nonpositiveinteger"}
NUMBER_TYPES = {"decimal", "double", "float"}
FORMATS = {"datetime": "date-time", "date": "date", "time": "time", "duration": "duration", "guid": "uuid",
           "base64binary": "byte", "anyuri": "uri"}


@dataclass
class _Bound:
    client: Any
    proxy: Any
    port: Any
    history: Any
    secrets: list[str]
    lock: threading.Lock
    wsdl: str


def _wsdl_location(settings: Settings, service: ServiceConfig, env: EnvironmentConfig) -> str:
    wsdl = service.value_for("wsdl", env.name)
    if not wsdl:
        raise ConfigError(f"Service '{service.name}' has no 'wsdl' for environment '{env.name}'")
    if "://" in wsdl:
        return wsdl
    path = os.path.expanduser(wsdl)
    if not os.path.isabs(path) and settings.path:
        path = os.path.join(os.path.dirname(settings.path), path)
    if not os.path.exists(path):
        raise ConfigError(f"WSDL file not found: {path}")
    return path


def _type_name(xsd_type: Any) -> str:
    qname = getattr(xsd_type, "qname", None)
    if qname is not None:
        return qname.localname
    name = getattr(xsd_type, "name", None) or type(xsd_type).__name__
    return str(name).split("}")[-1]


def _simple_schema(xsd_type: Any) -> dict[str, Any]:
    # walk up restrictions to the built-in base type
    base = xsd_type
    for _ in range(10):
        name = _type_name(base).lower()
        if name in INTEGER_TYPES | NUMBER_TYPES | {"boolean", "string"} | set(FORMATS):
            break
        parent = getattr(base, "base_class", None) or getattr(type(base), "__bases__", (None,))[0]
        if parent is None or parent is object:
            break
        base = parent() if isinstance(parent, type) else parent
    name = _type_name(base).lower()
    out: dict[str, Any] = {"xsd": _type_name(xsd_type)}
    if name in INTEGER_TYPES:
        out["type"] = "integer"
    elif name in NUMBER_TYPES:
        out["type"] = "number"
    elif name == "boolean":
        out["type"] = "boolean"
    else:
        out["type"] = "string"
        if name in FORMATS:
            out["format"] = FORMATS[name]
    return out


def type_schema(xsd_type: Any, depth: int = 0, seen: frozenset = frozenset()) -> dict[str, Any]:
    from zeep.xsd import ComplexType

    if not isinstance(xsd_type, ComplexType):
        return _simple_schema(xsd_type)
    name = _type_name(xsd_type)
    if depth > 8 or (name and name in seen and name != "ComplexType"):
        return {"type": "object", "xsd": name, "note": "recursive/deep type, not expanded"}
    seen = seen | {name}
    props: dict[str, Any] = {}
    required: list[str] = []
    for el_name, element in xsd_type.elements:
        el_type = getattr(element, "type", None)
        if el_type is None:  # xsd:any
            props[el_name] = {"description": "xsd:any - XML content"}
            continue
        sub = type_schema(el_type, depth + 1, seen)
        max_occurs = getattr(element, "max_occurs", 1)
        if max_occurs == "unbounded" or (isinstance(max_occurs, int) and max_occurs > 1):
            sub = {"type": "array", "items": sub}
        if getattr(element, "nillable", False):
            sub["nullable"] = True
        props[el_name] = sub
        if getattr(element, "min_occurs", 0) and not getattr(element, "nillable", False):
            required.append(el_name)
    for attr_name, attr in getattr(xsd_type, "attributes", []) or []:
        if getattr(attr, "type", None) is not None:
            props[f"_{attr_name}"] = {**_simple_schema(attr.type), "description": "XML attribute"}
    out: dict[str, Any] = {"type": "object", "properties": props}
    if name and name != "ComplexType":
        out["xsd"] = name
    if required:
        out["required"] = required
    return out


def example_from_schema(schema: dict[str, Any], depth: int = 0) -> Any:
    kind = schema.get("type")
    if kind == "object":
        if depth > 5:
            return {}
        return {k: example_from_schema(v, depth + 1) for k, v in (schema.get("properties") or {}).items()
                if not k.startswith("_")}
    if kind == "array":
        return [example_from_schema(schema.get("items") or {}, depth + 1)]
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    fmt = schema.get("format")
    return {"date-time": "2026-01-31T00:00:00", "date": "2026-01-31", "uuid": "00000000-0000-0000-0000-000000000000"
            }.get(fmt or "", "string")


def _message_schema(message: Any) -> dict[str, Any] | None:
    if message is None:
        return None
    body = getattr(message, "body", None)
    if body is None:
        return None
    el_type = getattr(body, "type", None)
    if el_type is None:
        return None
    return type_schema(el_type)


class SoapAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[tuple[str, str], _Bound] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._cache.clear()

    def bound(self, service: ServiceConfig, env: EnvironmentConfig) -> _Bound:
        key = (service.name, env.name)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = self._bind(service, env)
            return self._cache[key]

    def invalidate(self, service: str | None = None) -> None:
        with self._lock:
            for key in [k for k in self._cache if service is None or k[0] == service]:
                del self._cache[key]

    def _bind(self, service: ServiceConfig, env: EnvironmentConfig) -> _Bound:
        import zeep
        from zeep.plugins import HistoryPlugin
        from zeep.transports import Transport

        session, secrets = build_session(self.settings, service, env)
        timeout = service.timeout_s or env.timeout_s
        transport = Transport(session=session, timeout=timeout, operation_timeout=timeout)
        history = HistoryPlugin(maxlen=1)
        plugins: list[Any] = [history]
        if service.ws_addressing:
            from zeep.wsa import WsAddressingPlugin

            plugins.append(WsAddressingPlugin())
        wsse = None
        auth = service.auth_for(env.name)
        if auth.type == "ws_username_token":
            from zeep.wsse.username import UsernameToken

            wsse = UsernameToken(auth.user, secret_from_env(auth.password_env or "", f"services.{service.name}.auth"),
                                 use_digest=auth.use_digest)
        wsdl = _wsdl_location(self.settings, service, env)
        client = zeep.Client(wsdl, transport=transport, plugins=plugins, wsse=wsse,
                             settings=zeep.Settings(strict=False, xml_huge_tree=True))
        port = self._select_port(client, service)
        address = service.value_for("endpoint", env.name) or port.binding_options.get("address")
        if not address:
            raise ConfigError(f"Service '{service.name}': the WSDL has no address for port '{port.name}'; set "
                              f"'endpoint' for environment '{env.name}'")
        self.settings.check_host(address, service, env)
        proxy = client.create_service(port.binding.name.text, address)
        return _Bound(client, proxy, port, history, secrets, threading.Lock(), wsdl)

    @staticmethod
    def _select_port(client: Any, service: ServiceConfig) -> Any:
        from zeep.wsdl.bindings.soap import SoapBinding

        services = list(client.wsdl.services.values())
        if not services:
            raise ConfigError("The WSDL defines no service")
        if service.service_name:
            match = [s for s in services if s.name == service.service_name]
            if not match:
                raise ConfigError(f"WSDL service '{service.service_name}' not found; available: "
                                  f"{', '.join(s.name for s in services)}")
            services = match
        ports = [p for s in services for p in s.ports.values()]
        if service.port_name:
            match = [p for p in ports if p.name == service.port_name]
            if not match:
                raise ConfigError(f"WSDL port '{service.port_name}' not found; available: "
                                  f"{', '.join(p.name for p in ports)}")
            return match[0]
        soap_ports = [p for p in ports if isinstance(p.binding, SoapBinding)]
        if not soap_ports:
            raise ConfigError("The WSDL has no SOAP port (netTcp / namedPipe / webHttp only?) - ask for a "
                              "basicHttpBinding endpoint on the test environment")
        # Prefer SOAP 1.1 (basicHttpBinding) - it is what works without WCF-specific message security.
        soap11 = [p for p in soap_ports if type(p.binding).__name__ == "Soap11Binding"]
        return (soap11 or soap_ports)[0]

    # -- discovery ---------------------------------------------------------------------------------------------
    def describe_service(self, service: ServiceConfig, env: EnvironmentConfig) -> dict[str, Any]:
        b = self.bound(service, env)
        ports = []
        for svc in b.client.wsdl.services.values():
            for port in svc.ports.values():
                ports.append({"service": svc.name, "port": port.name, "binding": type(port.binding).__name__,
                              "address": port.binding_options.get("address"),
                              "selected": port is b.port})
        return {"wsdl": b.wsdl, "address": b.proxy._binding_options.get("address"), "ports": ports,
                "operations": sorted(b.port.binding._operations)}

    def describe_operation(self, service: ServiceConfig, env: EnvironmentConfig, operation: str) -> dict[str, Any]:
        b = self.bound(service, env)
        op = self._operation(b, operation)
        input_schema = _message_schema(op.input) or {"type": "object", "properties": {}}
        out: dict[str, Any] = {
            "operation": op.name,
            "soap_action": getattr(op, "soapaction", None),
            "style": getattr(op, "style", None),
            "input_schema": input_schema,
            "example_params": example_from_schema(input_schema),
            "output_schema": _message_schema(op.output),
            "signature": op.input.signature(as_output=False) if op.input is not None else None,
            "mutating": service.is_mutating(op.name),
        }
        return out

    @staticmethod
    def _operation(b: _Bound, operation: str) -> Any:
        ops = b.port.binding._operations
        if operation in ops:
            return ops[operation]
        lowered = {k.lower(): k for k in ops}
        if operation and operation.lower() in lowered:
            return ops[lowered[operation.lower()]]
        raise ConfigError(f"Operation '{operation}' not found. Available: {', '.join(sorted(ops))}")

    # -- call ----------------------------------------------------------------------------------------------------
    def call(self, service: ServiceConfig, env: EnvironmentConfig, operation: str,
             params: dict[str, Any] | None) -> dict[str, Any]:
        """Returns the raw (unmasked) step result. Never raises for SOAP faults or HTTP errors."""
        from lxml import etree
        from zeep.exceptions import Fault, TransportError
        from zeep.helpers import serialize_object

        b = self.bound(service, env)
        op = self._operation(b, operation)
        if service.is_mutating(op.name) and not env.allow_mutating:
            raise ConfigError(f"Operation '{op.name}' of '{service.name}' is marked as mutating; environment "
                              f"'{env.name}' has allow_mutating: false.")
        params = params or {}
        if not isinstance(params, dict):
            raise ValueError("'params' must be a JSON object (element name -> value)")
        result: dict[str, Any] = {"protocol": "soap", "service": service.name, "operation": op.name,
                                  "environment": env.name, "status": None, "fault": None, "body": None,
                                  "headers": {}}
        started = time.perf_counter()
        with b.lock:
            try:
                value = getattr(b.proxy, op.name)(**params)
                result["status"] = 200
                result["body"] = normalize(serialize_object(value, dict))
            except Fault as exc:
                detail = None
                if exc.detail is not None:
                    try:
                        detail = etree.tostring(exc.detail, encoding="unicode")
                    except Exception:  # noqa: BLE001
                        detail = str(exc.detail)
                code = exc.code
                result["status"] = 500
                result["fault"] = {"code": str(code).split("}")[-1].split(":")[-1] if code else None, "message": exc.message,
                                   "actor": exc.actor, "detail": detail}
            except TransportError as exc:
                result["status"] = exc.status_code
                result["error"] = {"type": "TransportError", "message": str(exc.message)[:2000],
                                   "body": (exc.content or b"").decode("utf-8", "replace")[:4000]
                                   if isinstance(exc.content, (bytes, bytearray)) else str(exc.content)[:4000]}
            except TypeError as exc:  # wrong parameter names
                raise ValueError(f"Invalid parameters for {op.name}: {exc}. Expected: "
                                 f"{op.input.signature(as_output=False) if op.input is not None else '()'}") from None
            finally:
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            raw = {}
            try:
                if b.history.last_sent:
                    raw["request"] = etree.tostring(b.history.last_sent["envelope"], encoding="unicode")
                if b.history.last_received:
                    raw["response"] = etree.tostring(b.history.last_received["envelope"], encoding="unicode")
                    result["headers"] = dict(b.history.last_received.get("http_headers") or {})
            except Exception:  # noqa: BLE001
                pass
            result["raw"] = raw
        return result
