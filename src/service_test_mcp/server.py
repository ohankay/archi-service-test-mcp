"""MCP server exposing SOAP/REST service-testing tools (stdio for VS Code, or streamable HTTP for other agents)."""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import connection, trace
from .config import ConfigError, env_setting
from .engine import Engine
from .normalize import json_ready
from .scenario.asserts import evaluate, find
from .scenario.model import ScenarioError
from .scenario.runner import Runner
from .importer.analyze import Importer
from .scenario.template import Context, TemplateError
from .definitions.checks import run_checks
from .definitions.generators import Generator
from .definitions.log_import import LogImporter
from .definitions.runner import TestRunner
from .definitions.schemas import KINDS
from .definitions.store import DefinitionError

log = logging.getLogger("service_test_mcp")

INSTRUCTIONS = """\
Service-layer test tools for TEST environments (mobile banking WCF and other services). Never use a terminal,
PowerShell, curl or Invoke-WebRequest for service calls - use these tools.
- list_tests: what is defined - systems, operations with their inputs, tests, test approaches (boundary value,
  equivalence, pairwise, negative ...). Use names from here; ask for a definition by kind and name to edit it.
- run_test(test, customer, environment): runs a defined test with the given customer number. Login, OTP, session and
  the request envelope are done by the server. Results: passed / failed / observation / error. An 'observation'
  (expected '?') is NOT a pass or fail: show the observed behaviour to the user and ask whether it is right, then
  update the test. 'ai_review' questions are for you to answer from the given data - clearly marked as your opinion.
- call_operation(operation, customer, inputs): try one operation (exploration).
- generate_tests(approach, operation): a draft table test made by code from the operation's definition. Show it,
  run it with run_test(content=...), save it with save_definition when the user approves.
- save_definition(kind, content): validated save of an operation / test / approach (/ system). New ones are 'draft'.
  To change an existing one, read it with list_tests(kind, name) and pass its 'version'.
- import_log(path): the server reads a mobile app log or HAR and proposes draft operations (preview); apply=true
  saves them as drafts after the user approves. save_secrets=true stores the user's own credentials from the log in
  the local secrets.yaml (never shown).
- Not defined -> say so and offer import_log; never invent operations, fields or results.
- Checks are short lines: success | failure | duration < 3 s | count X >= 1 | X is not empty | X = Y |
  X matches ^TR | X between a and b | X contains y | error code = CODE | for each LIST: A <= B. Prefer rules over
  fixed values for data that changes (balances, rates, dates).
- Results start with success, result and 'problem' (the failing step and why): report these first.
- Transparency: every result that made HTTP calls has 'flow' (one line per exchange: login step, URL, HTTP status
  or error phase, time, business result), 'trace' (the exchanges that went wrong: request and response) and
  'trace_file' (full trace JSON). When something fails, show the user the failing 'flow' line, the error 'phase',
  the cause 'chain' and 'likely_causes' - never only "an error occurred". Timeouts / connection errors ->
  check_connection.
- check_connection(system, environment): tries each route to the address (the current one, direct, the proxy
  Windows / the browser would use) step by step (DNS, TCP, TLS, HTTP); 'routes' shows which works, 'suggestion' what
  to change.
- Only credentials are hidden, shown as «secret: name». Production environments are refused. Answer the user in
  their language.
"""

mcp = FastMCP("archi-service-test", instructions=INSTRUCTIONS)
_engine: Engine | None = None
_engine_lock = threading.Lock()
_last: dict[str, Any] = {}
_vars: dict[str, Any] = {}   # session variables saved by 'extract' on soap_call / rest_call
TRACE_LIMIT = int(env_setting("STM_TRACE_CHARS", "STM_IZ_SINIR", "4000") or 4000)  # trace returned with a result
# whole tool result; larger ones are moved to a file by the client (VS Code) and agents often do not read it
RESULT_LIMIT = int(env_setting("STM_RESULT_CHARS", "STM_SONUC_SINIR", "8000") or 8000)
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
        out["call_with"] = "call_profile_operation"
    return out


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, DefinitionError):
        return {"success": False, "error": {"type": "DefinitionError", "problems": exc.problems}}
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
    # unexpected: show where it happened in the server code, so nothing is a black box
    out["error"]["chain"] = trace.cause_chain(exc)
    out["error"]["location"] = [f"{f.filename.replace(chr(92), '/').rsplit('/service_test_mcp/', 1)[-1]}:{f.lineno} "
                             f"{f.name}: {(f.line or '').strip()[:120]}"
                             for f in traceback.extract_tb(exc.__traceback__)[-8:]]
    return out


def _show_all_trace() -> bool:
    return (env_setting("STM_TRACE", "STM_IZ") or "").strip().lower() in ("all", "hepsi")


def _with_trace(result: dict[str, Any], records: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """Every HTTP exchange the tool made goes back with the result: readable lines, the exchanges, the trace file."""
    if not records:
        return result
    e = engine()
    # the verdict and the first problem first: agents read the start, clients cut or move long results
    head = {k: result[k] for k in ("success", "result") if k in result}
    problem = trace.problem(records)
    if problem:
        head["problem"] = problem
    result = {**head, **result}
    result["flow"] = [trace.line(r) for r in records]
    # details only for the exchanges that went wrong, short and without headers (STM_TRACE=all: all, in full);
    # the complete trace is always in the file
    show_all = _show_all_trace()
    shown = records if show_all else [r for r in records if trace.went_wrong(r)]
    if shown:
        result["trace"] = e.view(trace.full(shown) if show_all else trace.brief(shown),
                                 limit=60000 if show_all else TRACE_LIMIT)
    if len(shown) < len(records) or not show_all:
        result["trace_note"] = "Headers and the requests/responses of the exchanges that went fine are in trace_file."
    path = trace.write_file(e.view(records, limit=10_000_000), title)
    if path:
        result["trace_file"] = path

    # always within the limit (STM_TRACE=all too): a result the agent cannot read is not transparent
    def size() -> int:
        return len(json.dumps(json_ready(result), ensure_ascii=False, default=str))
    if size() > RESULT_LIMIT and show_all and shown:
        result["trace"] = e.view(trace.brief([r for r in records if trace.went_wrong(r)] or shown[-1:]),
                                 limit=TRACE_LIMIT)
    if size() > RESULT_LIMIT:
        result.pop("trace", None)
        result["trace_note"] = "The result is long: request/response details are only in trace_file."
    if size() > RESULT_LIMIT:
        result = e.view(result, limit=RESULT_LIMIT)
    return result


async def _call(fn: Callable[[], Any]) -> str:
    title = fn.__qualname__.split(".")[0]

    def guarded() -> Any:
        with trace.collect() as records:
            try:
                result = fn()
                if isinstance(result, dict) and "success" not in result:
                    result = {"success": True, **result}
                return _with_trace(result, records, title) if isinstance(result, dict) else result
            except Exception as exc:  # noqa: BLE001 - reported to the model as a structured error
                return _with_trace(engine().view(_error(exc)), records, title)

    result = await anyio.to_thread.run_sync(guarded)
    return json.dumps(json_ready(result), ensure_ascii=False, default=str, separators=(",", ":"))


# -- discovery -------------------------------------------------------------------------------------------------
@mcp.tool(annotations=READ, structured_output=False)
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


@mcp.tool(annotations=READ, structured_output=False)
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


@mcp.tool(annotations=READ, structured_output=False)
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


@mcp.tool(annotations=READ, structured_output=False)
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
@mcp.tool(annotations=CALL, structured_output=False)
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


@mcp.tool(annotations=CALL, structured_output=False)
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


@mcp.tool(annotations=READ, structured_output=False)
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


@mcp.tool(annotations=CALL, structured_output=False)
async def call_profile_operation(service: str, operation: str, params: dict[str, Any] | None = None,
                         test_customer: str | None = None, environment: str | None = None,
                         extract: dict[str, str] | None = None, assertions: list[dict[str, Any]] | None = None,
                         fresh_session: bool = False) -> str:
    """Call an operation of a profile service by name. Pass only the operation's own params (see
    describe_operation); the server adds the envelope and headers, logs in with the test customer when
    needed, renews the session and retries once if the server ended it. Values may use {{variables}} and
    {{test_customer.Field}}. test_customer: name from list_test_customers (default: the profile's default).
    extract = {name: JSONPath} saves session variables; assertions = optional list checked on the response.
    fresh_session forces a new login.
    Returns business success/errors, status, body (masked), elapsed_ms."""
    def run() -> dict[str, Any]:
        e = engine()
        ctx = _context(e, environment)
        ctx.keep = ("test_customer", "session")
        rendered = ctx.render(params) if params else None
        result = e.profile_call(service, operation, rendered, test_customer, environment, fresh_session=fresh_session)
        _last.clear()
        _last.update(result)
        out = {**e.result_view(result), **e.view(_extract(result, extract))}
        if assertions:
            checks = evaluate(assertions, result)
            out["assertions_passed"] = all(c["passed"] for c in checks)
            out["assertions"] = e.view(checks)
        return out
    return await _call(run)


@mcp.tool(annotations=READ, structured_output=False)
async def list_test_customers(service: str, environment: str | None = None) -> str:
    """Test customers of a profile service in an environment, the live sessions of this server and
    test_customers busy elsewhere (another VS Code window, agent or CI holds the login)."""
    def run() -> dict[str, Any]:
        e = engine()
        runtime = e.profile(service)
        env = e.settings.environment(environment)
        return e.view(runtime.status(env.name))
    return await _call(run)


@mcp.tool(annotations=READ, structured_output=False)
async def check_connection(system: str | None = None, environment: str | None = None) -> str:
    """Connection diagnosis when a call times out or cannot connect. Tries every route to the address - the one the
    MCP uses now, direct, and the proxy Windows / the browser would use (PAC) - each step by step (DNS, TCP, TLS, one
    HTTP request) with timings. 'routes' shows which works, 'suggestion' the line to put in the system definition.
    Sends no business request and needs no customer. system: system name (default: all); environment: default
    environment if omitted."""
    def run() -> dict[str, Any]:
        e = engine()
        names = [system] if system else [n for n, s in e.settings.services.items() if s.protocol == "rest"]
        results = [connection.check(e, n, environment) for n in names]
        path = trace.write_file(e.view({"systems": results}, limit=10_000_000), "check_connection")
        # short answer for the agent (long tool results get cut); everything is in the file
        return e.view({"success": all(r["success"] for r in results),
                       "systems": [connection.summary(r) for r in results], "details_file": path})
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def end_sessions(service: str | None = None, environment: str | None = None,
                       customer: str | None = None) -> str:
    """Log out and free the test customers so others (another window, agent, CI) can use them. Without arguments:
    every session of this server. Call it when the testing is done. service: a system name from list_tests
    (e.g. "Mobile Banking") - not the MCP server's name; leave it empty for all."""
    def run() -> dict[str, Any]:
        e = engine()
        everything = not service or service.strip().lower() in (mcp.name.lower(), "all", "*")
        names = list(e.profiles) if everything else [e.profile(service).service]
        ended = []
        for name in names:
            ended += [{"service": name, **x} for x in e.profiles[name].end_sessions(environment, customer)]
        return {"ended": ended}
    return await _call(run)


# -- definitions: the default tool set ----------------------------------------------------------------------------
def _operation_summary(r: Any) -> dict[str, Any]:
    v = r.data
    inputs = {}
    for name, spec in (v.get("inputs") or {}).items():
        text = spec.get("type") or "text"
        if spec.get("required"):
            text += ", required"
        if "default" in spec:
            text += f", default {spec['default']}"
        if spec.get("source"):
            text += f", if not given: from {spec['source']['operation']}"
        inputs[name] = text
    out = {"name": r.name, "kind": v.get("kind", "unknown"), "status": v.get("status", "draft"), "inputs": inputs}
    if v.get("description"):
        out["description"] = v["description"]
    if v.get("date_range"):
        out["date_range"] = v["date_range"]
    return out


@mcp.tool(annotations=READ, structured_output=False)
async def list_tests(kind: str | None = None, name: str | None = None, search: str | None = None) -> str:
    """What is defined: systems (environments), operations with inputs, tests and test approaches.
    kind = system | operation | test | approach narrows the list; with name you get that whole definition (YAML) and
    its 'version' (needed to save a change). search filters by text."""
    def run() -> dict[str, Any]:
        e = engine()
        store = e.store
        if name:
            if not kind:
                raise ValueError(f"give kind together with name ({', '.join(KINDS)})")
            r = store.get(kind, name)
            import yaml

            return e.view({"kind": kind, "name": r.name, "version": r.version, "file": str(r.path),
                           "content": yaml.safe_dump(r.data, allow_unicode=True, sort_keys=False, width=120)}, 60000)
        kinds = [kind] if kind else list(KINDS)
        out: dict[str, Any] = {"definitions_folder": str(store.root)}

        def keep(r: Any) -> bool:
            return not search or search.lower() in json.dumps(r.data, ensure_ascii=False).lower()

        if "system" in kinds:
            out["systems"] = [{"name": r.name, "environments": list((r.data.get("environments") or {})),
                               "loaded": r.name in e.profiles} for r in store.all("system") if keep(r)]
            out["default_environment"] = e.settings.default_environment
        if "operation" in kinds:
            out["operations"] = [_operation_summary(r) for r in store.all("operation") if keep(r)]
        if "test" in kinds:
            out["tests"] = [{"name": r.name, "status": r.data.get("status", "draft"),
                             "type": "table" if r.data.get("examples") else "steps",
                             "operations": [s["operation"] for s in r.data.get("steps") or []]
                             or [r.data.get("operation")],
                             "tags": r.data.get("tags") or [], "description": r.data.get("description", "")}
                            for r in store.all("test") if keep(r)]
        if "approach" in kinds:
            out["approaches"] = [{"name": r.name, "aliases": r.data.get("aliases") or [],
                                  "when_to_use": r.data.get("when_to_use", ""),
                                  "generated_by": "agent" if r.data["generator"] == "agent" else "code"}
                                 for r in store.all("approach") if keep(r)]
        if store.load_errors:
            out["invalid_definitions"] = store.load_errors
        if not any(store.all(k) for k in KINDS):
            out["note"] = f"No definitions. Folder: {store.root} (STM_DEFINITIONS changes it)."
        return e.view(out, 60000)
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def run_test(test: str | None = None, customer: str | None = None, environment: str | None = None,
                   content: str | None = None, inputs: dict[str, Any] | None = None,
                   tags: list[str] | None = None) -> str:
    """Run a defined test (by name) with a customer number, or a draft given as YAML 'content'. With 'tags' (and no
    test) every approved test having one of the tags runs. Returns per step/example: passed, failed (with the failed
    checks and a response summary), observation (expected '?': ask the user), error (could not run);
    and 'ai_review' questions for you."""
    def run() -> dict[str, Any]:
        e = engine()
        runner = TestRunner(e)
        if tags and not test and not content:
            wanted = {t.lower() for t in tags}
            tests = [t for t in e.store.all("test") if t.data.get("status") == "approved" and
                     wanted & {x.lower() for x in t.data.get("tags") or []}]
            if not tests:
                raise ValueError(f"no approved test tagged '{', '.join(tags)}'")
            results = [runner.run(t.name, None, customer, environment, inputs) for t in tests]
            summary = {s: sum(1 for r in results if r["result"] == s) for s in ("passed", "failed", "observations")}
            for r in results:
                if r["result"] == "passed":
                    r.pop("steps", None)
                    r.pop("examples", None)
            return e.view({"summary": {k: v for k, v in summary.items() if v}, "tests": results}, 60000)
        return e.view(runner.run(test, content, customer, environment, inputs), 60000)
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def call_operation(operation: str, customer: str | None = None, inputs: dict[str, Any] | None = None,
                         environment: str | None = None, checks: list[str] | None = None) -> str:
    """Call one defined operation by its name with friendly inputs, e.g.
    call_operation("Account transactions", customer="1000001", inputs={"start": "today-30"}). Defaults, dates and
    inputs taken from other operations are filled in; login is automatic. checks = optional check lines.
    Returns the business result, the inputs that were sent and the response."""
    def run() -> dict[str, Any]:
        e = engine()
        runner = TestRunner(e)
        op = e.store.get("operation", operation)
        env = e.settings.environment(environment).name
        result, effective = runner.call(op, inputs, customer, env, origin="tool")
        _last.clear()
        _last.update(result)
        business = result.get("business") or {}
        out: dict[str, Any] = {"operation": op.name, "environment": env, "customer": customer,
                               "business_success": bool(business.get("success")),
                               "input": e.view(effective), "duration_ms": result.get("elapsed_ms"),
                               "http": result.get("status")}
        if business.get("errors"):
            out["errors"] = business["errors"]
        if result.get("error"):
            out["connection_error"] = result["error"]
        if checks:
            res = run_checks([str(c) for c in checks], result, runner.error_paths(op))
            out["checks_passed"] = all(c["passed"] for c in res)
            out["checks"] = e.view(res)
        out["response"] = e.view(result.get("body"), 30000)
        return out
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def save_definition(kind: str, content: str, version: str | None = None) -> str:
    """Validate and save a definition. kind = operation | test | approach | system; content = the YAML text.
    Problems (unknown operation or input, a check line that does not parse ...) are returned and nothing is saved.
    New definitions get status: draft unless the user approved them (status: approved). To change an existing one
    pass the 'version' you read with list_tests(kind, name)."""
    def run() -> dict[str, Any]:
        import yaml

        e = engine()
        try:
            data = yaml.safe_load(content)
        except Exception as exc:  # noqa: BLE001
            raise DefinitionError([f"cannot read YAML: {exc}"]) from None
        if not isinstance(data, dict):
            raise DefinitionError(["the content must be a mapping (key: value)"])
        record = e.store.save(kind, data, version)
        e.reload_definitions()
        return {"saved": record.name, "kind": kind, "status": record.data.get("status"), "version": record.version,
                "file": str(record.path)}
    return await _call(run)


@mcp.tool(annotations=READ, structured_output=False)
async def generate_tests(approach: str, operation: str, common_inputs: dict[str, Any] | None = None) -> str:
    """Make a draft table test for an operation with a test approach (e.g. "boundary value", "pairwise", "negative",
    "equivalence"; list_tests shows them). Rule-based approaches are generated by code from the operation's
    definition (deterministic); 'agent' approaches return their rules for you to write the table. Missing limits are
    reported in 'notes'. Nothing is saved or called: show it, run it with run_test(content=...), save on approval."""
    def run() -> dict[str, Any]:
        import yaml

        e = engine()
        out = Generator(e.store).generate(approach, operation, common_inputs)
        if "test" in out:
            out["content"] = yaml.safe_dump(out.pop("test"), allow_unicode=True, sort_keys=False, width=120)
        return e.view(out, 60000)
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def import_log(path: str | None = None, content: str | None = None, environment: str | None = None,
                     apply: bool = False, save_secrets: bool = False) -> str:
    """Read a traffic log (mobile app transaction log or HAR) ON THE SERVER and propose draft operations for what is
    not defined yet (inputs, fixed fields, kind). The raw log never reaches you. apply=false is a preview; after the
    user approves, apply=true saves the drafts. save_secrets=true copies the system's secret values (app passwords,
    the common test password, the fixed OTP) from the log into the local secrets.yaml of the detected environment."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(LogImporter(e).run(path, content, environment, apply, save_secrets), 60000)
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def import_traffic_log(path: str | None = None, content: str | None = None, service: str | None = None,
                             environment: str | None = None, apply: bool = False,
                             scenario_path: str | None = None, format: str | None = None) -> str:
    """Read a traffic log (mobile app transaction log or HAR) ON THE SERVER and propose catalog changes: new /
    changed / unchanged operations, kinds, session need, test customer and credential fields, values chained from
    earlier responses, and a draft scenario of the flow. The raw log never reaches you; values are masked.
    Service and environment are found from the URLs (give them only if the host is not in services.yaml).
    apply=false (default) is a preview - show it to the user; apply=true writes the catalog.
    scenario_path writes the draft scenario YAML to a file (under scenario_root)."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(Importer(e).run(path, content, service, environment, apply, scenario_path, format), 60000)
    return await _call(run)


@mcp.tool(annotations=READ, structured_output=False)
async def session_variables(clear: bool = False) -> str:
    """Show (masked) or clear the session variables saved by 'extract' on soap_call / rest_call."""
    def run() -> dict[str, Any]:
        e = engine()
        if clear:
            _vars.clear()
        return e.view({"variables": dict(_vars)})
    return await _call(run)


@mcp.tool(annotations=READ, structured_output=False)
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
@mcp.tool(annotations=READ, structured_output=False)
async def validate_scenario(path: str | None = None, content: str | None = None) -> str:
    """Validate scenario YAML (a file, a folder, or inline 'content'): structure, services, assertion syntax,
    JSONPath, and variables used before they are defined. Does not call any service."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(Runner(e).validate(path, content))
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
async def run_scenario(path: str | None = None, content: str | None = None, environment: str | None = None,
                       variables: dict[str, Any] | None = None, verbosity: str = "failures") -> str:
    """Run one scenario file (or inline YAML 'content'). Steps run in order; extract passes values to later steps.
    verbosity: summary | failures (default: details only for failed steps) | full."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(Runner(e).run(path, content, environment, variables, verbosity=verbosity), 60000)
    return await _call(run)


@mcp.tool(annotations=CALL, structured_output=False)
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


@mcp.tool(annotations=READ, structured_output=False)
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


# The default tool set (STM_TOOLSET=basic): definitions, tests, approaches, connection check. Plain names.
BASIC_TOOL_NAMES = ("list_tests", "run_test", "call_operation", "save_definition", "generate_tests", "import_log",
                    "check_connection", "end_sessions")
# STM_TOOLSET=expert adds the raw SOAP/REST, profile and YAML-scenario tools.
EXPERT_TOOL_NAMES = ("list_services", "describe_service", "describe_operation", "check_service", "list_operations",
                    "call_profile_operation", "list_test_customers", "import_traffic_log", "soap_call", "rest_call",
                    "session_variables", "check_last_response", "validate_scenario", "run_scenario",
                    "run_test_suite", "reload_config")
TOOL_NAMES = BASIC_TOOL_NAMES + EXPERT_TOOL_NAMES
TOOLSETS = {"basic": BASIC_TOOL_NAMES, "expert": TOOL_NAMES}


def toolset_name(value: str | None = None) -> str:
    name = (value or os.environ.get("STM_TOOLSET") or "basic").strip().lower()
    # earlier names still accepted, so existing mcp.json files keep working
    name = {"default": "basic", "temel": "basic", "all": "expert", "uzman": "expert"}.get(name, name)
    if name not in TOOLSETS:
        raise SystemExit(f"STM_TOOLSET must be one of {', '.join(TOOLSETS)}")
    return name


def apply_toolset(value: str | None = None) -> tuple[str, ...]:
    """Remove the tools that are not in the selected set (before the server starts)."""
    keep = TOOLSETS[toolset_name(value)]
    for name in TOOL_NAMES:
        if name not in keep:
            try:
                mcp.remove_tool(name)
            except Exception:  # noqa: BLE001 - already removed
                pass
    return keep


def run_stdio() -> None:
    apply_toolset()
    mcp.run("stdio")


def run_http(host: str, port: int, token: str | None) -> None:
    """Streamable HTTP transport (endpoint /mcp) for agents outside VS Code. Bearer token required if set."""
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    apply_toolset()
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
