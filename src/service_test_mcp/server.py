"""MCP server exposing SOAP/REST service-testing tools (stdio for VS Code, or streamable HTTP for other agents)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import ConfigError
from .engine import Engine
from .normalize import json_ready
from .scenario.asserts import evaluate, find
from .scenario.model import ScenarioError
from .scenario.runner import Runner
from .importer.analyze import Importer
from .scenario.template import Context, TemplateError

log = logging.getLogger("service_test_mcp")

INSTRUCTIONS = """\
Service testing tools for WCF/SOAP and REST services on TEST environments. Never use a terminal, PowerShell, curl or
Invoke-WebRequest for service calls - use these tools.
- Start with list_services. Services with a profile (e.g. mobile banking WCF) are called by OPERATION NAME:
  list_operations -> describe_operation -> call_operation(service, operation, params, persona). Login, OTP, session
  renewal, the request envelope and headers are done by the server; never send session keys or tokens yourself.
  persona = named test customer (list_personas); one login per customer at a time, the server enforces it.
- Other services: describe_service / describe_operation, then soap_call / rest_call. Use extract={"token":
  "$.Token"} to keep values as session variables and send them later as {{token}}.
- Traffic logs (mobile app logs, HAR): import_traffic_log(path) - the server reads and masks the file, you get a
  summary. It is a preview; after the user approves call it again with apply=true to update the catalog.
- check_last_response evaluates assertions on the last call deterministically.
- Repeatable tests are YAML scenario files. Profile steps: service + operation + params (+ persona); others:
  operation/params (SOAP) or method/path (REST); assert, extract. validate_scenario, then run_scenario /
  run_test_suite.
- Assertions: success (profile business rule), fault, status, responseTimeMs, header, schema, or path (JSONPath) +
  one operator: equals, notEquals, equalsIgnoreCase, contains, notContains, startsWith, endsWith, matches, in, notIn,
  exists, notEmpty, isNull, type, numeric {gt,gte,lt,lte,eq,ne,between,tolerance}, count, length, schema.
- Variables: {{name}}, {{env.var}}, {{secret.STM_SECRET_X}}, {{persona.Field}}, {{$uuid}}, {{$today}}, {{$now}},
  {{$date(+1)}}, {{$random_digits(8)}}.
- Sensitive values in responses are masked (***1234); assertions and extract use the real values.
- Mutating operations run only where the environment allows them. Production environments are refused.
"""

mcp = FastMCP("service-test-mcp", instructions=INSTRUCTIONS)
_engine: Engine | None = None
_engine_lock = threading.Lock()
_last: dict[str, Any] = {}
_vars: dict[str, Any] = {}   # session variables saved by 'extract' on soap_call / rest_call
_EXPECTED = (ConfigError, ScenarioError, TemplateError, ValueError, FileNotFoundError, PermissionError)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CALL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def set_engine(value: Engine | None) -> None:
    global _engine
    with _engine_lock:
        _engine = value
    _last.clear()
    _vars.clear()


def _context(e: Engine, environment: str | None) -> Context:
    e._require_config()
    env = e.settings.environment(environment)
    return Context(variables=_vars, env_variables=e.env_variables(env.name), on_secret=e.masker.add_secret)


def _extract(result: dict[str, Any], extract: dict[str, str] | None) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    errors = []
    for name, path in (extract or {}).items():
        values = find(result.get("body"), str(path))
        if not values:
            errors.append(f"{name}: {path} not found")
            continue
        saved[name] = values[0] if len(values) == 1 else values
    _vars.update(saved)
    out: dict[str, Any] = {"saved_variables": saved} if saved else {}
    if errors:
        out["extract_errors"] = errors
    return out


def _service_public(e: Engine, svc: Any, environment: str | None) -> dict[str, Any]:
    out = svc.public(environment)
    runtime = e.profiles.get(svc.name)
    if runtime is not None:
        out.update(runtime.profile.public(environment or e.settings.default_environment))
        out["call_with"] = "call_operation"
    return out


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ScenarioError):
        return {"success": False, "error": {"type": "ScenarioError", "problems": exc.problems}}
    if isinstance(exc, _EXPECTED):
        return {"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    log.debug("tool error", exc_info=exc)
    message = str(exc)
    hint = None
    if "SSL" in message or "CERTIFICATE" in message.upper():
        hint = "TLS verification failed: set verify_tls to the CA bundle (.pem) path of the environment, or false on test"
    elif "Max retries" in message or "Failed to establish" in message or "NameResolution" in message:
        hint = "Host not reachable: check the URL, VPN/proxy (environment 'proxy') and firewall"
    elif "401" in message:
        hint = "Unauthorized: check the service's auth type (windows / ntlm / basic / bearer) and credentials"
    out: dict[str, Any] = {"success": False, "error": {"type": type(exc).__name__, "message": message[:3000]}}
    if hint:
        out["error"]["hint"] = hint
    return out


async def _call(fn: Callable[[], Any]) -> str:
    def guarded() -> Any:
        try:
            result = fn()
            if isinstance(result, dict) and "success" not in result:
                result = {"success": True, **result}
            return result
        except Exception as exc:  # noqa: BLE001 - reported to the model as a structured error
            return engine().view(_error(exc))

    result = await anyio.to_thread.run_sync(guarded)
    return json.dumps(json_ready(result), ensure_ascii=False, default=str, separators=(",", ":"))


# -- discovery -------------------------------------------------------------------------------------------------
@mcp.tool(annotations=READ)
async def list_services(environment: str | None = None) -> str:
    """List configured services (SOAP/REST), environments and the default environment. Start here."""
    def run() -> dict[str, Any]:
        e = engine()
        s = e.settings
        out: dict[str, Any] = {"config_file": s.path, "default_environment": s.default_environment,
                               "environments": {n: {"description": v.description, "allow_mutating": v.allow_mutating,
                                                    "variables": sorted(e.env_variables(n))}
                                                for n, v in s.environments.items()},
                               "services": [_service_public(e, svc, environment) for svc in s.services.values()]}
        if s.broken:
            out["broken_services"] = s.broken
        if e.load_error:
            out["config_error"] = e.load_error
        if not s.services and not e.load_error:
            out["note"] = ("No services configured. Set STM_CONFIG in mcp.json to a services.yaml "
                           "(template: templates/services.yaml).")
        return e.view(out)
    return await _call(run)


@mcp.tool(annotations=READ)
async def describe_service(service: str, environment: str | None = None) -> str:
    """SOAP: WSDL ports, selected address and operation names. REST: base_url and OpenAPI operations (if configured)."""
    def run() -> dict[str, Any]:
        e = engine()
        svc, env = e.resolve(service, environment)
        if svc.name in e.profiles:
            runtime = e.profiles[svc.name]
            ops = runtime.profile.catalog.operations.values()
            return e.view({"service": _service_public(e, svc, env.name), "environment": env.name,
                           "operations_by_kind": {k: sorted(o.name for o in ops if o.kind == k)
                                                  for k in ("read", "mutating", "auth", "unknown")},
                           "envelope_fields": sorted(runtime.profile.envelope_keys()),
                           "note": "call_operation adds the envelope, login and session; pass only the params."})
        details = e.soap.describe_service(svc, env) if svc.protocol == "soap" else e.rest.describe_service(svc, env)
        return e.view({"service": svc.public(env.name), "environment": env.name, **details}, mask=False)
    return await _call(run)


@mcp.tool(annotations=READ)
async def describe_operation(service: str, operation: str, environment: str | None = None) -> str:
    """Input/output schema of an operation. Profile services: catalog entry (params example, request/response schema,
    kind, session). SOAP: operation name -> input_schema, example_params, output_schema.
    REST: 'METHOD /path' or operationId from the OpenAPI document."""
    def run() -> dict[str, Any]:
        e = engine()
        svc, env = e.resolve(service, environment)
        if svc.name in e.profiles:
            op = e.profiles[svc.name].operation(operation)
            out = {"operation": op.name, **op.to_data(), "mutating": op.mutating,
                   "allowed_here": not op.mutating or env.allow_mutating,
                   "example_call": {"service": svc.name, "operation": op.name, "params": op.params}}
            return e.view(out, 60000)
        if svc.protocol == "soap":
            return e.view(e.soap.describe_operation(svc, env, operation), 60000, mask=False)
        return e.view(e.rest.describe_operation(svc, env, operation), 60000, mask=False)
    return await _call(run)


@mcp.tool(annotations=READ)
async def check_service(service: str | None = None, environment: str | None = None) -> str:
    """Connectivity check without calling an operation: SOAP loads the WSDL and binds the port; REST requests base_url.
    Without 'service' every service is checked."""
    def run() -> dict[str, Any]:
        e = engine()
        e._require_config()
        names = [service] if service else list(e.settings.services)
        report: dict[str, Any] = {}
        for name in names:
            try:
                svc, env = e.resolve(name, environment)
                if svc.protocol == "soap":
                    info = e.soap.describe_service(svc, env)
                    report[svc.name] = {"ok": True, "environment": env.name, "address": info.get("address"),
                                        "operations": len(info.get("operations") or [])}
                else:
                    res = e.rest.call(svc, env, "GET", "")
                    ok = res.get("error") is None
                    report[svc.name] = {"ok": ok, "environment": env.name, "http_status": res.get("status"),
                                        **({"error": res["error"]} if not ok else {})}
            except Exception as exc:  # noqa: BLE001
                report[name] = {"ok": False, **_error(exc)["error"]}
        for name, why in e.settings.broken.items():
            if not service or service == name:
                report[name] = {"ok": False, "type": "ConfigError", "message": why}
        return e.view({"services": report, "all_ok": all(v.get("ok") for v in report.values())})
    return await _call(run)


# -- calls -----------------------------------------------------------------------------------------------------
@mcp.tool(annotations=CALL)
async def soap_call(service: str, operation: str, params: dict[str, Any] | None = None,
                    environment: str | None = None, extract: dict[str, str] | None = None,
                    include_raw: bool = False) -> str:
    """Call a SOAP/WCF operation. params = JSON object of the operation's input elements (see describe_operation).
    Values may use {{variables}}. extract = {name: JSONPath} saves values from the response as session variables
    (e.g. {"token": "$.Token"}) so later calls can send {{token}} even though it is masked in the output.
    Returns status, fault (code/message/detail) or body (JSON), elapsed_ms. include_raw adds the SOAP envelopes."""
    def run() -> dict[str, Any]:
        e = engine()
        rendered = _context(e, environment).render(params or {})
        result = e.soap_call(service, operation, rendered, environment)
        _last.clear()
        _last.update(result)
        return {**e.result_view(result, include_raw=include_raw), **e.view(_extract(result, extract))}
    return await _call(run)


@mcp.tool(annotations=CALL)
async def rest_call(service: str, method: str, path: str, query: dict[str, Any] | None = None,
                    headers: dict[str, Any] | None = None, body: Any = None, body_text: str | None = None,
                    environment: str | None = None, extract: dict[str, str] | None = None,
                    include_headers: bool = False) -> str:
    """Call a REST endpoint. path is relative to the service base_url (e.g. /hesaplar/123). body = JSON value,
    body_text = raw text (XML, form). path, query, headers and body may use {{variables}}
    (e.g. headers {"Authorization": "Bearer {{token}}"}). extract = {name: JSONPath} saves session variables.
    Returns status, body (parsed JSON or text), elapsed_ms."""
    def run() -> dict[str, Any]:
        e = engine()
        ctx = _context(e, environment)
        result = e.rest_call(service, method, ctx.render(path), ctx.render(query), ctx.render(headers),
                             ctx.render(body), ctx.render(body_text), None, environment)
        _last.clear()
        _last.update(result)
        return {**e.result_view(result, include_headers=include_headers), **e.view(_extract(result, extract))}
    return await _call(run)


@mcp.tool(annotations=READ)
async def list_operations(service: str, search: str | None = None, kind: str | None = None,
                          tag: str | None = None) -> str:
    """Operations in the catalog of a profile service (name, kind read/mutating/auth/unknown, needs session,
    param names). search = part of the name/description or a wildcard (Get*); kind / tag filter."""
    def run() -> dict[str, Any]:
        e = engine()
        runtime = e.profile(service)
        ops = runtime.profile.catalog.search(search, tag, kind)
        out: dict[str, Any] = {"service": runtime.service, "total": len(runtime.profile.catalog.operations),
                               "operations": [o.summary() for o in ops[:200]]}
        if len(ops) > 200:
            out["note"] = f"{len(ops)} matches, first 200 shown: narrow with 'search'"
        if not runtime.profile.catalog.operations:
            out["note"] = "The catalog is empty: import a traffic log with import_traffic_log."
        return e.view(out)
    return await _call(run)


@mcp.tool(annotations=CALL)
async def call_operation(service: str, operation: str, params: dict[str, Any] | None = None,
                         persona: str | None = None, environment: str | None = None,
                         extract: dict[str, str] | None = None, assertions: list[dict[str, Any]] | None = None,
                         fresh_session: bool = False) -> str:
    """Call an operation of a profile service by name. Pass only the operation's own params (see
    describe_operation); the server adds the envelope and headers, logs in with the persona (test customer) when
    needed, renews the session and retries once if the server ended it. Values may use {{variables}} and
    {{persona.Field}}. persona: name from list_personas (default: the profile's default). extract = {name: JSONPath}
    saves session variables; assertions = optional list checked on the response. fresh_session forces a new login.
    Returns business success/errors, status, body (masked), elapsed_ms."""
    def run() -> dict[str, Any]:
        e = engine()
        ctx = _context(e, environment)
        ctx.keep = ("persona", "session")
        rendered = ctx.render(params) if params else None
        result = e.profile_call(service, operation, rendered, persona, environment, fresh_session=fresh_session)
        _last.clear()
        _last.update(result)
        out = {**e.result_view(result), **e.view(_extract(result, extract))}
        if assertions:
            checks = evaluate(assertions, result)
            out["assertions_passed"] = all(c["passed"] for c in checks)
            out["assertions"] = e.view(checks)
        return out
    return await _call(run)


@mcp.tool(annotations=READ)
async def list_personas(service: str, environment: str | None = None) -> str:
    """Test customers (personas) of a profile service in an environment, the live sessions of this server and
    personas busy elsewhere (another VS Code window, agent or CI holds the login)."""
    def run() -> dict[str, Any]:
        e = engine()
        runtime = e.profile(service)
        env = e.settings.environment(environment)
        return e.view(runtime.status(env.name))
    return await _call(run)


@mcp.tool(annotations=CALL)
async def end_sessions(service: str | None = None, environment: str | None = None,
                       persona: str | None = None) -> str:
    """Log out and release persona leases so others can use those test customers. Without arguments: all
    sessions of this server."""
    def run() -> dict[str, Any]:
        e = engine()
        names = [e.profile(service).service] if service else list(e.profiles)
        ended = []
        for name in names:
            ended += [{"service": name, **x} for x in e.profiles[name].end_sessions(environment, persona)]
        return {"ended": ended}
    return await _call(run)


@mcp.tool(annotations=CALL)
async def import_traffic_log(path: str | None = None, content: str | None = None, service: str | None = None,
                             environment: str | None = None, apply: bool = False,
                             scenario_path: str | None = None, format: str | None = None) -> str:
    """Read a traffic log (mobile app transaction log or HAR) ON THE SERVER and propose catalog changes: new /
    changed / unchanged operations, kinds, session need, persona and credential fields, values chained from earlier
    responses, and a draft scenario of the flow. The raw log never reaches you; values are masked.
    Service and environment are found from the URLs (give them only if the host is not in services.yaml).
    apply=false (default) is a preview - show it to the user; apply=true writes the catalog.
    scenario_path writes the draft scenario YAML to a file (under scenario_root)."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(Importer(e).run(path, content, service, environment, apply, scenario_path, format), 60000)
    return await _call(run)


@mcp.tool(annotations=READ)
async def session_variables(clear: bool = False) -> str:
    """Show (masked) or clear the session variables saved by 'extract' on soap_call / rest_call."""
    def run() -> dict[str, Any]:
        e = engine()
        if clear:
            _vars.clear()
        return e.view({"variables": dict(_vars)})
    return await _call(run)


@mcp.tool(annotations=READ)
async def check_last_response(assertions: list[dict[str, Any]]) -> str:
    """Evaluate assertions against the last call_operation / soap_call / rest_call response (real values).
    Use it to try assertions before writing them into a scenario file."""
    def run() -> dict[str, Any]:
        e = engine()
        if not _last:
            raise ValueError("No call yet: run call_operation, soap_call or rest_call first")
        checks = evaluate(assertions, _last)
        return e.view({"service": _last.get("service"), "operation": _last.get("operation") or
                       f"{_last.get('method')} {_last.get('path')}", "passed": all(c["passed"] for c in checks),
                       "assertions": checks})
    return await _call(run)


# -- scenarios -------------------------------------------------------------------------------------------------
@mcp.tool(annotations=READ)
async def validate_scenario(path: str | None = None, content: str | None = None) -> str:
    """Validate scenario YAML (a file, a folder, or inline 'content'): structure, services, assertion syntax,
    JSONPath, and variables used before they are defined. Does not call any service."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(Runner(e).validate(path, content))
    return await _call(run)


@mcp.tool(annotations=CALL)
async def run_scenario(path: str | None = None, content: str | None = None, environment: str | None = None,
                       variables: dict[str, Any] | None = None, verbosity: str = "failures") -> str:
    """Run one scenario file (or inline YAML 'content'). Steps run in order; extract passes values to later steps.
    verbosity: summary | failures (default: details only for failed steps) | full."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(Runner(e).run(path, content, environment, variables, verbosity=verbosity), 60000)
    return await _call(run)


@mcp.tool(annotations=CALL)
async def run_test_suite(path: str, environment: str | None = None, tags: list[str] | None = None,
                         verbosity: str = "summary", junit_path: str | None = None) -> str:
    """Run every scenario under a folder (recursively), optionally only those with one of 'tags'. Returns a summary
    and failed assertions; junit_path writes a JUnit XML report."""
    def run() -> dict[str, Any]:
        e = engine()
        out = Runner(e).run(path, None, environment, None, tags, verbosity, junit_path)
        if verbosity == "summary":
            for r in out["results"]:
                r["steps"] = [s for s in r["steps"] if s.get("status") in ("failed", "error")]
        return e.view(out, 60000)
    return await _call(run)


@mcp.tool(annotations=READ)
async def reload_config() -> str:
    """Re-read services.yaml after it was edited (no server restart needed)."""
    def run() -> dict[str, Any]:
        e = engine()
        e.reload()
        _last.clear()
        _vars.clear()
        out = {"config_file": e.settings.path, "services": sorted(e.settings.services),
               "environments": sorted(e.settings.environments)}
        if e.settings.broken:
            out["broken_services"] = e.settings.broken
        if e.load_error:
            out["config_error"] = e.load_error
        return out
    return await _call(run)


TOOL_NAMES = ("list_services", "describe_service", "describe_operation", "check_service", "list_operations",
              "call_operation", "list_personas", "end_sessions", "import_traffic_log", "soap_call", "rest_call",
              "session_variables", "check_last_response", "validate_scenario", "run_scenario", "run_test_suite",
              "reload_config")
# The smaller set for weaker models (standalone agent --toolset profile): no raw SOAP/REST.
PROFILE_TOOL_NAMES = ("list_services", "list_operations", "describe_operation", "call_operation", "list_personas",
                      "end_sessions", "import_traffic_log", "check_last_response", "validate_scenario",
                      "run_scenario", "run_test_suite")


def run_stdio() -> None:
    mcp.run("stdio")


def run_http(host: str, port: int, token: str | None) -> None:
    """Streamable HTTP transport (endpoint /mcp) for agents outside VS Code. Bearer token required if set."""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    local = host in ("127.0.0.1", "localhost", "::1")
    if not local:
        if not token:
            raise SystemExit("Listening on a non-local address needs a token: set STM_HTTP_TOKEN (clients send "
                             "'Authorization: Bearer <token>').")
        # Remote clients use the machine's name in the Host header; the bearer token protects the endpoint.
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp.settings.host, mcp.settings.port = host, port
    app = mcp.streamable_http_app()
    if token:
        inner = app

        async def guarded(scope, receive, send):  # type: ignore[no-untyped-def]
            if scope["type"] == "http":
                headers = dict(scope.get("headers") or [])
                if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                    return
            await inner(scope, receive, send)

        app = guarded  # type: ignore[assignment]
    uvicorn.run(app, host=host, port=port, log_level="warning")
