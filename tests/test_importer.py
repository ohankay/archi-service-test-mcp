"""Traffic log import: parsing, environment detection, catalog proposal / apply / diff, secrets never leak."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service_test_mcp import demo_mobile
from service_test_mcp.config import ConfigError
from service_test_mcp.importer import schema as sch
from service_test_mcp.importer.analyze import Importer, guess_kind
from service_test_mcp.importer.parsers import detect, parse_har, parse_traffic, parse_transaction_log
from service_test_mcp.profile.model import Catalog
from service_test_mcp.scenario.runner import Runner

SVC = "MobilBankacilik"


def _empty_catalog(engine) -> Path:
    path = engine.profiles[SVC].profile.catalog_path
    path.write_text("operations: {}\n", encoding="utf-8")
    engine.profiles[SVC].profile.catalog = Catalog.load(path)
    return path


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def test_transaction_log_pairing():
    text = "\n".join([
        "*" * 40, "Date: 01-01-2026 10:00:00", "Method: post", "Method Name: A", "URL: http://h/x/A",
        'RequestData: {"x": 1}', "*" * 40, "Method Name: A", "URL: http://h/x/A", 'ResponseData: {"ok": true}',
        "*" * 40, "Method Name: A", "URL: http://h/x/A", 'ResponseData: {"ok": true}',            # logged twice
        "*" * 40, "Method: post", "Method Name: B", "URL: http://h/x/B", "RequestData: {\"y\": 2.50}",  # no response
        "*" * 40, "Method Name: C", "URL: http://h/x/C", "ResponseData: {}",                      # orphan
        "*" * 40, "Method: post", "Method Name: D", "URL: http://h/x/D", "RequestData: not json",
    ])
    assert detect(text) == "transaction_log"
    exchanges, warnings = parse_transaction_log(text)
    assert [e.name for e in exchanges] == ["A", "B", "D"]
    a, b, d = exchanges
    assert a.response_body == {"ok": True} and a.extra_responses == 1 and a.status == 200
    assert b.has_response is False and str(b.request_body["y"]) == "2.50"
    assert d.request_body == "not json"
    assert any("no logged response" in w for w in warnings) and any("without a matching" in w for w in warnings)


def test_har_parser():
    har = {"log": {"entries": [{"startedDateTime": "2026-01-01T10:00:00Z",
                                "request": {"method": "post", "url": "https://h/api/X.svc/Do?a=1",
                                            "headers": [{"name": "X-Token", "value": "t"}],
                                            "postData": {"text": "{\"a\": 1}"}},
                                "response": {"status": 200, "headers": [], "content": {"text": "{\"ok\": 1}"}}}]}}
    text = json.dumps(har)
    assert detect(text) == "har"
    (ex,), _ = parse_har(text)
    assert ex.method == "POST" and ex.name == "Do" and ex.request_headers == {"X-Token": "t"}
    assert ex.request_body == {"a": 1} and ex.response_body == {"ok": 1} and ex.status == 200
    with pytest.raises(ValueError, match="Unrecognized log format"):
        parse_traffic("hello")


def test_schema_merge_and_diff():
    a = sch.infer({"x": 1, "y": "s", "z": [{"k": 1}]})
    b = sch.infer({"x": 1.5, "z": [{"k": 2, "n": True}]})
    m = sch.merge(a, b)
    assert m["properties"]["x"]["type"] == "number" and m["required"] == ["x", "z"]
    assert m["properties"]["z"]["items"]["properties"]["n"]["type"] == "boolean"
    changes = sch.diff(a, sch.infer({"x": "text", "y": "s", "w": 1, "z": [{"k": 1, "n": False}]}))
    assert "$.x: type integer -> string" in changes and "$.w: new field (integer)" in changes
    assert "$.z[*].n: new field (boolean)" in changes
    assert sch.diff(a, sch.infer({"x": 2})) == []                    # absence is not a removal


def test_guess_kind():
    assert guess_kind("GetDashBoard", set(), None) == "read"
    assert guess_kind("HasUnreadNotification", set(), None) == "read"
    assert guess_kind("ExecuteMoneyTransfer", set(), None) == "mutating"
    assert guess_kind("InitialLogin", set(), None) == "auth"
    assert guess_kind("Logout", set(), "Logout") == "auth"
    assert guess_kind("ApplicationPreData", set(), None) == "unknown"


def test_import_preview_apply_and_reimport(mobile):
    catalog_path = _empty_catalog(mobile)
    log = Path(mobile.settings.path).parent / "logs" / "ornek-prep-transaction.log"
    importer = Importer(mobile)

    preview = importer.run(str(log))
    assert preview["applied"] is False and preview["format"] == "transaction_log"
    rep = preview["services"][0]
    assert rep["environments"] == {"prep": 8}                       # base URL of prep
    assert rep["summary"] == {"new": 8} and preview["noise_skipped"] == {"InsertClientLog": 1}
    ops = {o["operation"]: o for o in rep["operations"]}
    assert ops["ExecuteMoneyTransfer"]["kind"] == "mutating" and ops["ExecuteMoneyTransfer"]["session"] is True
    assert ops["GetDashBoardMobileInformation"]["kind"] == "read"
    assert ops["ApplicationPreData"]["session"] is False and ops["ApplicationPreData"]["kind"] == "unknown"
    assert ops["LoginForMobileBranch"]["kind"] == "auth" and ops["LoginForMobileBranch"]["session"] is False
    assert ops["OtpLoginCompleteNew"]["credential_fields"] == ["GeneratedOtp"]
    assert "UserId" in ops["LoginForMobileBranch"]["test_customer_fields"]
    # the app sends the session key of the latest login (the LoginForMobileBranch one only reached noise calls)
    assert {"field": "ExtUSessionKey", "from": ["InitialLogin $.SessionKey"]} in rep["envelope_values_from_responses"]
    assert "steps:" in rep["draft_scenario"] and "LoginForMobileBranch" not in rep["draft_scenario"]
    assert any("apply=true" in s for s in preview["next_steps"])
    assert catalog_path.read_text("utf-8").strip() == "operations: {}"   # preview writes nothing

    # nothing secret or personal reaches the model or the catalog file
    text = _dump(mobile.view(preview))
    for secret in (demo_mobile.APP_PASSWORD, demo_mobile.OTP, "ACT-1000001-x9", "TR320010009999901234567890"):
        assert secret not in text
    applied = importer.run(str(log), apply=True)
    assert applied["applied"] is True
    written = catalog_path.read_text("utf-8")
    for secret in (demo_mobile.APP_PASSWORD, "ACT-1000001-x9", "TR320010009999901234567890", "ExtUSessionKey"):
        assert secret not in written
    cat = mobile.profiles[SVC].profile.catalog
    assert cat.get("GetDashBoardMobileInformation").params == {"MainResourceCode": "DASHBOARD"}
    assert cat.get("InitialLogin").params["CustomerId"] == "{{test_customer.CustomerId}}"

    # hand edits survive a re-import; the same log is "unchanged"
    cat.operations["ApplicationPreData"].kind = "read"
    cat.operations["ApplicationPreData"].description = "elle yazildi"
    cat.save()
    again = importer.run(str(log), apply=True)["services"][0]
    assert again["summary"] == {"unchanged": 8}
    reloaded = mobile.profiles[SVC].profile.catalog.get("ApplicationPreData")
    assert reloaded.kind == "read" and reloaded.description == "elle yazildi" and reloaded.seen["samples"] == 2


def test_contract_change_is_reported(mobile):
    _empty_catalog(mobile)
    log = Path(mobile.settings.path).parent / "logs" / "ornek-prep-transaction.log"
    importer = Importer(mobile)
    importer.run(str(log), apply=True)
    changed = log.read_text("utf-8").replace('"MinVersion": "7.0.0",', '"MinVersion": 7,\n    "Banner": "yeni",')
    rep = importer.run(content=changed)["services"][0]
    op = next(o for o in rep["operations"] if o["operation"] == "ApplicationPreData")
    assert op["status"] == "changed"
    assert "response.Value.MinVersion: type string -> integer" in op["changes"]
    assert "response.Value.Banner: new field (string)" in op["changes"]


def test_environment_from_base_url_and_unknown_hosts(mobile):
    _empty_catalog(mobile)
    log = (Path(mobile.settings.path).parent / "logs" / "ornek-prep-transaction.log").read_text("utf-8")
    port = log.split("127.0.0.1:")[1].split("/")[0]
    as_test = Importer(mobile).run(content=log.replace("/prep/MobileBranch/", "/test/MobileBranch/"))
    assert as_test["services"][0]["environments"] == {"test": 8}
    elsewhere = log.replace(f"http://127.0.0.1:{port}", "https://mobilprep.banka.local")
    unknown = Importer(mobile).run(content=elsewhere)
    assert unknown["matched"] == 0 and "mobilprep.banka.local" in unknown["warnings"][-1]
    assert unknown["outside_profile_services"]
    accepted = Importer(mobile).run(content=elsewhere, service=SVC, environment="prep")
    assert accepted["matched"] == 8 and any("matched by base path" in w for w in accepted["warnings"])
    assert accepted["services"][0]["environments"] == {"prep": 8}
    with pytest.raises(ConfigError, match="production"):
        Importer(mobile).run(content=log, environment="prod")


def test_draft_scenario_file_and_run(mobile):
    _empty_catalog(mobile)
    log = Path(mobile.settings.path).parent / "logs" / "ornek-prep-transaction.log"
    rep = Importer(mobile).run(str(log), apply=True, scenario_path="scenarios/_taslak.yaml")["services"][0]
    path = Path(rep["draft_scenario_file"])
    assert path.exists() and path.name == "_taslak.yaml"
    text = path.read_text("utf-8").replace("'**********************7890'", "'{{env.hedef_iban}}'")
    out = Runner(mobile).run(content=text, environment="prep")
    assert out["summary"]["failed"] == 0, out
    with pytest.raises(PermissionError):
        Importer(mobile).run(str(log), scenario_path="/tmp/disari.yaml")
