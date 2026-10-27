"""Unit tests: no network."""

from __future__ import annotations

from decimal import Decimal

import pytest

from service_test_mcp.config import ConfigError, MaskingConfig, parse_settings
from service_test_mcp.masking import Masker, luhn, valid_tckn
from service_test_mcp.normalize import json_ready
from service_test_mcp.scenario.asserts import evaluate_one, validate_assertion
from service_test_mcp.scenario.model import ScenarioError, parse_scenario
from service_test_mcp.scenario.template import Context, TemplateError

BASE = {"environments": {"test": {}}, "default_environment": "test"}


# -- config -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["prod", "PROD", "production", "canli", "live"])
def test_production_environment_refused(name):
    with pytest.raises(ConfigError, match="production"):
        parse_settings({"environments": {name: {}}})


def test_unknown_setting_is_an_error():
    with pytest.raises(ConfigError, match="unknown setting"):
        parse_settings({"environments": {"test": {"timeout": 3}}})


def test_broken_service_does_not_break_others():
    s = parse_settings({**BASE, "services": {
        "ok": {"protocol": "rest", "base_url": "http://a.test/api"},
        "bad": {"protocol": "soap"},
        "badauth": {"protocol": "rest", "base_url": "http://b.test", "auth": {"type": "basic", "user": "u"}}}})
    assert list(s.services) == ["ok"]
    assert "wsdl" in s.broken["bad"] and "password_env" in s.broken["badauth"]
    with pytest.raises(ConfigError, match="misconfigured"):
        s.service("bad")


def test_host_allowlist():
    s = parse_settings({**BASE, "blocked_hosts": ["*.prod.local"], "services": {
        "api": {"protocol": "rest", "base_url": "https://api.test.local/v1", "allowed_hosts": ["auth.test.local"]}}})
    svc, env = s.service("api"), s.environment(None)
    s.check_host("https://api.test.local/v1/x", svc, env)
    s.check_host("https://auth.test.local/token", svc, env)
    with pytest.raises(ConfigError, match="allowlist"):
        s.check_host("https://evil.example.com/", svc, env)
    with pytest.raises(ConfigError, match="scheme"):
        s.check_host("ftp://api.test.local/", svc, env)


def test_mutating_rules():
    s = parse_settings({**BASE, "services": {
        "soap": {"protocol": "soap", "wsdl": "x.wsdl", "mutating_operations": ["Transfer*"]},
        "rest": {"protocol": "rest", "base_url": "http://a/", "mutating_operations": ["POST /transfer*"]},
        "ro": {"protocol": "rest", "base_url": "http://a/", "read_only": True}}})
    assert s.service("soap").is_mutating("TransferYap") and not s.service("soap").is_mutating("GetBakiye")
    assert s.service("rest").is_mutating("/transfers", "post") and not s.service("rest").is_mutating("/x", "POST")
    assert s.service("ro").is_mutating("/x", "PUT") and not s.service("ro").is_mutating("/x", "GET")


def test_secret_from_env_message(monkeypatch):
    from service_test_mcp.config import secret_from_env

    monkeypatch.delenv("STM_PW_X", raising=False)
    with pytest.raises(ConfigError, match=r"input:stm-pw-x"):
        secret_from_env("STM_PW_X", "services.a.auth")
    monkeypatch.setenv("STM_PW_X", "${input:stm-pw-x}")  # unresolved VS Code placeholder
    with pytest.raises(ConfigError):
        secret_from_env("STM_PW_X", "services.a.auth")


# -- masking ----------------------------------------------------------------------------------------------------
def test_checksums():
    assert valid_tckn("10000000146") and not valid_tckn("12345678901")
    assert luhn("4111111111111111") and not luhn("4111111111111112")


def test_masking_values_and_fields():
    m = Masker(MaskingConfig(), secrets=["S3cr3t!pw"])
    out = m.mask({"aciklama": "TCKN 10000000146 IBAN TR330006100519786457841326 kart 4111 1111 1111 1111",
                  "sifre": "abc", "Token": "xyz", "telefon": "05321234567", "no": 12345678901,
                  "mesaj": "parola S3cr3t!pw idi", "tutar": Decimal("10.5"), "musteriNo": "1000001"})
    text = out["aciklama"]
    assert "10000000146" not in text and "0146" in text
    assert "TR330006100519786457841326" not in text and "1326" in text
    assert "4111 1111 1111 1111" not in text
    assert out["sifre"] == "***" and out["Token"] == "***"
    assert out["telefon"].endswith("4567") and "0532" not in out["telefon"]
    assert out["no"] == 12345678901  # not a valid TCKN -> untouched
    assert "S3cr3t!pw" not in out["mesaj"]
    assert out["musteriNo"] == "1000001"


def test_unmask_fields_and_extra_fields():
    m = Masker(MaskingConfig(fields=["musteriNo"], unmask_fields=["iban"]))
    out = m.mask({"musteriNo": "1000001", "iban": "TR330006100519786457841326"})
    assert out["musteriNo"] == "***0001" and out["iban"] == "TR330006100519786457841326"


def test_masking_disabled():
    assert Masker(MaskingConfig(enabled=False)).mask({"sifre": "x"}) == {"sifre": "x"}


def test_json_ready_decimals():
    assert json_ready({"a": Decimal("1500.50"), "b": Decimal("2"), "c": Decimal("0.1")}) == {"a": 1500.5, "b": 2,
                                                                                            "c": 0.1}


# -- templates --------------------------------------------------------------------------------------------------
def test_template_types_and_paths(monkeypatch):
    seen = []
    ctx = Context({"n": 5, "o": {"t": "abc", "l": [{"x": 1}]}}, {"musteri": "100"}, on_secret=seen.append)
    assert ctx.render("{{n}}") == 5
    assert ctx.render("no-{{n}}") == "no-5"
    assert ctx.render("{{o.l[0].x}}") == 1
    assert ctx.render({"m": "{{env.musteri}}"}) == {"m": "100"}
    assert len(ctx.render("{{$uuid}}")) == 36 and len(ctx.render("{{$random_digits(6)}}")) == 6
    monkeypatch.setenv("STM_SECRET_PW", "gizli123")
    assert ctx.render("{{secret.STM_SECRET_PW}}") == "gizli123" and seen == ["gizli123"]
    monkeypatch.setenv("PATH_LIKE", "x")
    with pytest.raises(TemplateError, match="STM_SECRET_"):
        ctx.render("{{secret.PATH_LIKE}}")
    with pytest.raises(TemplateError, match="Unknown variable"):
        ctx.render("{{yok}}")


# -- assertions -------------------------------------------------------------------------------------------------
RESULT = {"status": 200, "fault": None, "elapsed_ms": 120, "headers": {"Content-Type": "application/json"},
          "body": {"Basarili": True, "Bakiye": Decimal("1500.50"), "Kod": "00", "Tarih": "2026-10-26T10:00:00",
                   "Liste": [{"d": "TRY", "t": 1}, {"d": "USD", "t": 2}], "Bos": "", "Null": None}}


@pytest.mark.parametrize("spec,ok", [
    ({"path": "$.Basarili", "equals": True}, True),
    ({"path": "$.Basarili", "equals": "true"}, True),
    ({"path": "$.Bakiye", "equals": 1500.5}, True),
    ({"path": "$.Bakiye", "numeric": {"gte": 1000, "lt": 2000}}, True),
    ({"path": "$.Bakiye", "numeric": {"eq": 1500.49, "tolerance": 0.01}}, True),
    ({"path": "$.Bakiye", "numeric": {"gt": 2000}}, False),
    ({"path": "$.Bakiye", "type": "decimal"}, True),
    ({"path": "$.Kod", "type": "number"}, False),
    ({"path": "$.Tarih", "type": "datetime"}, True),
    ({"path": "$.Kod", "in": ["00", "01"]}, True),
    ({"path": "$.Kod", "matches": "^0\\d$"}, True),
    ({"path": "$.Liste", "count": 2}, True),
    ({"path": "$.Liste[*].d", "in": ["TRY", "USD"], "all": True}, True),
    ({"path": "$.Liste[*].d", "equals": "TRY", "all": True}, False),
    ({"path": "$.Liste[?(@.d == 'USD')].t", "equals": 2}, True),
    ({"path": "$.Liste[*].d", "contains": "USD"}, True),
    ({"path": "$.Yok", "exists": False}, True),
    ({"path": "$.Yok", "equals": 1}, False),
    ({"path": "$.Bos", "notEmpty": True}, False),
    ({"path": "$.Null", "isNull": True}, True),
    ({"path": "$.Kod", "length": {"eq": 2}}, True),
    ({"path": "$", "schema": {"type": "object", "required": ["Kod"]}}, True),
    ({"status": [200, 201]}, True),
    ({"status": {"gte": 400}}, False),
    ({"fault": "none"}, True),
    ({"fault": "any"}, False),
    ({"responseTimeMs": {"lt": 100}}, False),
    ({"header": "content-type", "contains": "json"}, True),
])
def test_assertions(spec, ok):
    res = evaluate_one(spec, RESULT)
    assert res["passed"] is ok, res


def test_fault_assertion_details():
    result = {"fault": {"code": "Client", "message": "Kullanıcı adı veya şifre hatalı"}, "body": None}
    assert evaluate_one({"fault": {"code": "client", "message_contains": "HATALI"}}, result)["passed"]
    assert not evaluate_one({"fault": {"code": "Server"}}, result)["passed"]


def test_assertion_validation_messages():
    assert validate_assertion({"path": "$.a"})
    assert validate_assertion({"path": "$.a", "equals": 1, "contains": 2})
    assert validate_assertion({"path": "$.a", "type": "sayi"})
    assert validate_assertion({"foo": 1})
    assert not validate_assertion({"path": "$.a", "eq": 1})  # alias
    assert evaluate_one({"path": "$.Kod", "equals": "99", "message": "Kod 00 olmali"}, RESULT)["message"].startswith(
        "Kod 00 olmali")


# -- scenario model ---------------------------------------------------------------------------------------------
PROTOCOLS = {"S": "soap", "R": "rest"}


def test_scenario_validation_collects_all_problems():
    with pytest.raises(ScenarioError) as info:
        parse_scenario({"id": "x", "foo": 1, "steps": [
            {"service": "S", "method": "GET"},
            {"service": "R", "path": "/a/{{token}}", "assert": [{"path": "$[", "equals": 1}]},
            {"service": "Yok", "operation": "A"}]}, PROTOCOLS)
    text = "\n".join(info.value.problems)
    for part in ("unknown key(s) foo", "needs 'operation'", "method not valid for a SOAP", "'{{token}}' is not defined",
                 "invalid JSONPath", "unknown service 'Yok'"):
        assert part in text, (part, text)


def test_scenario_extract_defines_variables():
    s = parse_scenario({"id": "x", "steps": [
        {"service": "S", "operation": "Login", "extract": {"token": "$.Token"}},
        {"service": "R", "path": "/a", "headers": {"Authorization": "Bearer {{token}}"}}]}, PROTOCOLS)
    assert [st.name for st in s.steps] == ["step 1", "step 2"]


def test_agent_files_are_in_sync_with_the_prompt_source():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("sync_agent_files", root / "scripts" / "sync_agent_files.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for target in mod.TARGETS:
        assert target.read_text(encoding="utf-8") == mod.render(), \
            f"{target.name} is stale: run python scripts/sync_agent_files.py"
