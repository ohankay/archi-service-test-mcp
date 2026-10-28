"""MCP server exposing SOAP/REST service-testing tools (stdio for VS Code, or streamable HTTP for other agents)."""

from __future__ import annotations

import json
import logging
import os
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
from .tanim.depo import TanimHatasi
from .tanim.kosucu import Kosucu
from .tanim.logdan import LogdanTanim
from .tanim.semalar import KLASORLER
from .tanim.uretec import Uretec

log = logging.getLogger("service_test_mcp")

INSTRUCTIONS = """\
Service-layer test tools for TEST environments (mobile banking WCF and other services). Never use a terminal,
PowerShell, curl or Invoke-WebRequest for service calls - use these tools.
- list_tests: what is defined - systems, operations (işlemler) with their inputs, tests, test approaches
  (sınır değer, eşdeğerlik, pairwise, negatif ...). Use names from here; ask for a definition by name to edit it.
- run_test(test, customer, environment): runs a defined test with the given customer number. Login, OTP, session and
  the request envelope are done by the server. Results: geçti / kaldı / gözlem. A 'gözlem' (expected '?') is NOT
  a pass or fail: show the observed behaviour to the user and ask whether it is correct, then update the test.
  'ai_yorum' questions are for you to answer from the given data - clearly marked as your opinion.
- call_operation(operation, customer, inputs): try one operation (exploration).
- generate_tests(approach, operation): a draft table test made by code from the operation's definition. Show it,
  run it with run_test(content=...), save it with save_definition when the user approves.
- save_definition(kind, content): validated save of an islem / test / yaklasim (/ sistem). New ones are 'taslak'.
  To change an existing one, read it with list_tests(kind, name) and pass its 'surum'.
- import_log(path): the server reads a mobile app log or HAR and proposes draft operations (preview); apply=true
  saves them as drafts after the user approves. save_secrets=true stores the user's own credentials from the log in
  the local gizli.yaml (never shown).
- Not defined -> say so and offer import_log; never invent operations, fields or results.
- Checks are short lines: başarılı | başarısız | süre < 3 sn | X sayısı >= 1 | X boş değil | X = Y | X eşleşir ^TR |
  X arasında a ve b | X içerir y | hata kodu = KOD | her LISTE için A <= B. Prefer rules over fixed values for data
  that changes (balances, rates, dates).
- Values shown as *** are masked. Production environments are refused.
"""

mcp = FastMCP("archi-service-test", instructions=INSTRUCTIONS)
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
        out["call_with"] = "call_profile_operation"
    return out


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, TanimHatasi):
        return {"success": False, "error": {"type": "TanimHatasi", "problems": exc.problems}}
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


@mcp.tool(annotations=READ)
async def list_test_customers(service: str, environment: str | None = None) -> str:
    """Test customers of a profile service in an environment, the live sessions of this server and
    test_customers busy elsewhere (another VS Code window, agent or CI holds the login)."""
    def run() -> dict[str, Any]:
        e = engine()
        runtime = e.profile(service)
        env = e.settings.environment(environment)
        return e.view(runtime.status(env.name))
    return await _call(run)


@mcp.tool(annotations=CALL)
async def end_sessions(service: str | None = None, environment: str | None = None,
                       customer: str | None = None) -> str:
    """Log out and free the test customers so others (another window, agent, CI) can use them. Without arguments:
    every session of this server. Call it when the testing is done."""
    def run() -> dict[str, Any]:
        e = engine()
        names = [e.profile(service).service] if service else list(e.profiles)
        ended = []
        for name in names:
            ended += [{"service": name, **x} for x in e.profiles[name].end_sessions(environment, customer)]
        return {"ended": ended}
    return await _call(run)


# -- definitions (tanimlar): the default tool set ----------------------------------------------------------------
def _ozet_islem(k: Any) -> dict[str, Any]:
    v = k.veri
    girdiler = {}
    for ad, g in (v.get("girdiler") or {}).items():
        text = g.get("tip") or "metin"
        if g.get("zorunlu"):
            text += ", zorunlu"
        if "varsayilan" in g:
            text += f", varsayılan {g['varsayilan']}"
        if g.get("kaynak"):
            text += f", verilmezse: {g['kaynak']['islem']}"
        girdiler[ad] = text
    out = {"ad": k.ad, "tur": v.get("tur", "bilinmiyor"), "durum": v.get("durum", "taslak"), "girdiler": girdiler}
    if v.get("aciklama"):
        out["aciklama"] = v["aciklama"]
    if v.get("tarih_araligi"):
        out["tarih_araligi"] = v["tarih_araligi"]
    return out


@mcp.tool(annotations=READ)
async def list_tests(kind: str | None = None, name: str | None = None, search: str | None = None) -> str:
    """What is defined: systems (environments), operations (işlemler) with inputs, tests and test approaches.
    kind = sistem | islem | test | yaklasim narrows the list; with name you get that whole definition (YAML) and its
    'surum' (needed to save a change). search filters by text."""
    def run() -> dict[str, Any]:
        e = engine()
        depo = e.depo
        if name:
            if not kind:
                raise ValueError("name ile birlikte kind verin (sistem, islem, test, yaklasim)")
            k = depo.gerekli(kind, name)
            import yaml

            return e.view({"tur": kind, "ad": k.ad, "surum": k.surum, "dosya": str(k.yol),
                           "icerik": yaml.safe_dump(k.veri, allow_unicode=True, sort_keys=False, width=120)}, 60000)
        kinds = [kind] if kind else list(KLASORLER)
        out: dict[str, Any] = {"tanim_klasoru": str(depo.kok)}

        def keep(k: Any) -> bool:
            return not search or search.lower() in json.dumps(k.veri, ensure_ascii=False).lower()

        if "sistem" in kinds:
            out["sistemler"] = [{"ad": k.ad, "ortamlar": list((k.veri.get("ortamlar") or {})),
                                 "calisiyor": k.ad in e.profiles} for k in depo.hepsi("sistem") if keep(k)]
            out["varsayilan_ortam"] = e.settings.default_environment
        if "islem" in kinds:
            out["islemler"] = [_ozet_islem(k) for k in depo.hepsi("islem") if keep(k)]
        if "test" in kinds:
            out["testler"] = [{"test": k.ad, "durum": k.veri.get("durum", "taslak"),
                               "tur": "tablo" if k.veri.get("ornekler") else "adımlı",
                               "islemler": [a["islem"] for a in k.veri.get("adimlar") or []] or [k.veri.get("islem")],
                               "etiketler": k.veri.get("etiketler") or [], "aciklama": k.veri.get("aciklama", "")}
                              for k in depo.hepsi("test") if keep(k)]
        if "yaklasim" in kinds:
            out["yaklasimlar"] = [{"yaklasim": k.ad, "diger_adlar": k.veri.get("diger_adlar") or [],
                                   "ne_zaman": k.veri.get("ne_zaman", ""),
                                   "uretim": "ajan" if k.veri["uretici"] == "ajan" else "otomatik (kodla)"}
                                  for k in depo.hepsi("yaklasim") if keep(k)]
        if depo.yukleme_hatalari:
            out["hatali_tanimlar"] = depo.yukleme_hatalari
        if not any(depo.hepsi(t) for t in KLASORLER):
            out["not"] = f"Tanım yok. Klasör: {depo.kok} (STM_TANIMLAR ile değiştirilebilir)."
        return e.view(out, 60000)
    return await _call(run)


@mcp.tool(annotations=CALL)
async def run_test(test: str | None = None, customer: str | None = None, environment: str | None = None,
                   content: str | None = None, inputs: dict[str, Any] | None = None,
                   tags: list[str] | None = None) -> str:
    """Run a defined test (by name) with a customer number, or a draft given as YAML 'content'. With 'tags' (and no
    test) every approved test having one of the tags runs. Returns per step/row: geçti, kaldı (with the failed
    checks and a masked response summary), gözlem (expected '?': ask the user), hata (could not run);
    and 'ai_yorum' questions for you."""
    def run() -> dict[str, Any]:
        e = engine()
        k = Kosucu(e)
        if tags and not test and not content:
            wanted = {t.lower() for t in tags}
            tests = [t for t in e.depo.hepsi("test") if t.veri.get("durum") == "onayli" and
                     wanted & {x.lower() for x in t.veri.get("etiketler") or []}]
            if not tests:
                raise ValueError(f"'{', '.join(tags)}' etiketli onaylı test yok")
            results = [k.kos(t.ad, None, customer, environment, inputs) for t in tests]
            ozet = {s: sum(1 for r in results if r["sonuc"] == s) for s in ("geçti", "kaldı", "gözlem var")}
            for r in results:
                if r["sonuc"] == "geçti":
                    r.pop("adimlar", None)
                    r.pop("satirlar", None)
            return e.view({"ozet": {k2: v for k2, v in ozet.items() if v}, "testler": results}, 60000)
        return e.view(k.kos(test, content, customer, environment, inputs), 60000)
    return await _call(run)


@mcp.tool(annotations=CALL)
async def call_operation(operation: str, customer: str | None = None, inputs: dict[str, Any] | None = None,
                         environment: str | None = None, checks: list[str] | None = None) -> str:
    """Call one defined operation (işlem) by its name with friendly inputs, e.g.
    call_operation("Hesap hareketleri", customer="1000001", inputs={"baslangic": "bugün-30"}). Defaults, dates and
    inputs taken from other operations are filled in; login is automatic. checks = optional check lines.
    Returns business result, the inputs that were sent and a masked response."""
    def run() -> dict[str, Any]:
        e = engine()
        k = Kosucu(e)
        islem = e.depo.gerekli("islem", operation)
        env = e.settings.environment(environment).name
        result, etkin = k.cagir(islem, inputs, customer, env, origin="tool")
        _last.clear()
        _last.update(result)
        out: dict[str, Any] = {"islem": islem.ad, "ortam": env, "musteri": customer,
                               "basarili": bool((result.get("business") or {}).get("success")),
                               "girdi": e.view(etkin), "sure_ms": result.get("elapsed_ms"),
                               "http": result.get("status")}
        if (result.get("business") or {}).get("errors"):
            out["hatalar"] = result["business"]["errors"]
        if result.get("error"):
            out["baglanti_hatasi"] = result["error"]
        if checks:
            from .tanim.kontrol import kontrol_et

            res = kontrol_et([str(c) for c in checks], result, k.hata_yollari(islem))
            out["kontroller_gecti"] = all(c["gecti"] for c in res)
            out["kontroller"] = e.view(res)
        out["yanit"] = e.view(result.get("body"), 30000)
        return out
    return await _call(run)


@mcp.tool(annotations=CALL)
async def save_definition(kind: str, content: str, version: str | None = None) -> str:
    """Validate and save a definition. kind = islem | test | yaklasim | sistem; content = the YAML text.
    Problems (unknown operation or input, a check line that does not parse ...) are returned and nothing is saved.
    New definitions get durum: taslak unless the user approved them (durum: onayli). To change an existing one pass
    the 'surum' you read with list_tests(kind, name)."""
    def run() -> dict[str, Any]:
        import yaml

        e = engine()
        try:
            data = yaml.safe_load(content)
        except Exception as exc:  # noqa: BLE001
            raise TanimHatasi([f"YAML okunamadı: {exc}"]) from None
        if not isinstance(data, dict):
            raise TanimHatasi(["içerik bir eşleme (anahtar: değer) olmalı"])
        kayit = e.depo.kaydet(kind, data, version)
        e.reload_definitions()
        return {"kaydedildi": kayit.ad, "tur": kind, "durum": kayit.veri.get("durum"), "surum": kayit.surum,
                "dosya": str(kayit.yol)}
    return await _call(run)


@mcp.tool(annotations=READ)
async def generate_tests(approach: str, operation: str, common_inputs: dict[str, Any] | None = None) -> str:
    """Make a draft table test for an operation with a test approach (e.g. "sınır değer", "pairwise", "negatif",
    "eşdeğerlik"; list_tests shows them). Rule-based approaches are generated by code from the operation's
    definition (deterministic); 'ajan' approaches return their rules for you to write the table. Missing limits are
    reported in 'notlar'. Nothing is saved or called: show it, run it with run_test(content=...), save on approval."""
    def run() -> dict[str, Any]:
        import yaml

        e = engine()
        out = Uretec(e.depo).uret(approach, operation, common_inputs)
        if "test" in out:
            out["icerik"] = yaml.safe_dump(out.pop("test"), allow_unicode=True, sort_keys=False, width=120)
        return e.view(out, 60000)
    return await _call(run)


@mcp.tool(annotations=CALL)
async def import_log(path: str | None = None, content: str | None = None, environment: str | None = None,
                     apply: bool = False, save_secrets: bool = False) -> str:
    """Read a traffic log (mobile app transaction log or HAR) ON THE SERVER and propose draft operations for what is
    not defined yet (inputs, fixed fields, kind). The raw log never reaches you. apply=false is a preview; after the
    user approves, apply=true saves the drafts. save_secrets=true copies the system's secret values (app passwords,
    the common test password, the fixed OTP) from the log into the local gizli.yaml of the detected environment."""
    def run() -> dict[str, Any]:
        e = engine()
        return e.view(LogdanTanim(e).calistir(path, content, environment, apply, save_secrets), 60000)
    return await _call(run)


@mcp.tool(annotations=CALL)
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


# The default tool set (STM_TOOLSET=temel): definitions, tests, approaches. Seven tools, plain names.
TEMEL_TOOL_NAMES = ("list_tests", "run_test", "call_operation", "save_definition", "generate_tests", "import_log",
                    "end_sessions")
# STM_TOOLSET=uzman adds the raw SOAP/REST, profile and YAML-scenario tools.
UZMAN_TOOL_NAMES = ("list_services", "describe_service", "describe_operation", "check_service", "list_operations",
                    "call_profile_operation", "list_test_customers", "import_traffic_log", "soap_call", "rest_call",
                    "session_variables", "check_last_response", "validate_scenario", "run_scenario",
                    "run_test_suite", "reload_config")
TOOL_NAMES = TEMEL_TOOL_NAMES + UZMAN_TOOL_NAMES
TOOLSETS = {"temel": TEMEL_TOOL_NAMES, "uzman": TOOL_NAMES}


def toolset_name(value: str | None = None) -> str:
    name = (value or os.environ.get("STM_TOOLSET") or "temel").strip().lower()
    name = {"basic": "temel", "default": "temel", "profile": "temel", "expert": "uzman", "all": "uzman"}.get(name,
                                                                                                         name)
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
