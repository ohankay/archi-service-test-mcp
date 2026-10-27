"""Integration tests against the in-process demo SOAP (WCF style) and REST services."""

from __future__ import annotations

import json

import pytest

from service_test_mcp.config import ConfigError
from service_test_mcp.scenario.runner import Runner

from .conftest import write_config


def test_soap_discovery(engine):
    svc, env = engine.resolve("HesapServisi", None)
    info = engine.soap.describe_service(svc, env)
    assert info["operations"] == ["GetBakiye", "Login", "TransferYap"]
    assert info["ports"][0]["binding"] == "Soap11Binding"
    op = engine.soap.describe_operation(svc, env, "getbakiye")  # case-insensitive
    assert op["input_schema"]["required"] == ["token", "musteriNo"]
    bakiye = op["output_schema"]["properties"]["GetBakiyeResult"]["properties"]
    assert bakiye["ToplamBakiyeTRY"]["type"] == "number"
    assert bakiye["Hesaplar"]["properties"]["Hesap"]["type"] == "array"
    assert op["example_params"] == {"token": "string", "musteriNo": "string"}


def test_soap_call_fault_and_masking(engine):
    ok = engine.soap_call("HesapServisi", "Login", {"kullaniciAdi": "demo", "sifre": "Demo123!"})
    assert ok["fault"] is None and ok["body"]["Basarili"] is True and len(ok["body"]["Token"]) == 32
    bal = engine.soap_call("HesapServisi", "GetBakiye", {"token": ok["body"]["Token"], "musteriNo": "1000001"})
    hesaplar = bal["body"]["Hesaplar"]["Hesap"]
    assert str(hesaplar[0]["Bakiye"]) == "1500.50"
    view = engine.result_view(bal)
    assert view["body"]["Hesaplar"]["Hesap"][0]["Iban"].endswith("1326") and "*" in \
        view["body"]["Hesaplar"]["Hesap"][0]["Iban"]
    bad = engine.soap_call("HesapServisi", "Login", {"kullaniciAdi": "demo", "sifre": "x"})
    assert bad["fault"]["code"] == "Client" and "hatalı" in bad["fault"]["message"]
    assert "CLIENT-001" in bad["fault"]["detail"]
    assert engine.result_view(bad)["success"] is False
    with pytest.raises(ValueError, match="Expected"):
        engine.soap_call("HesapServisi", "Login", {"yanlis": 1})


def test_mutating_blocked(engine):
    with pytest.raises(ConfigError, match="mutating"):
        engine.soap_call("HesapServisi", "TransferYap", {})


def test_mutating_allowed_when_environment_allows(tmp_path, demo_port, monkeypatch):
    cfg = write_config(tmp_path, demo_port)
    cfg.write_text(cfg.read_text("utf-8").replace("allow_mutating: false", "allow_mutating: true"), encoding="utf-8")
    monkeypatch.setenv("STM_CONFIG", str(cfg))
    monkeypatch.setenv("STM_HOME", str(tmp_path / "home"))
    from service_test_mcp.engine import Engine

    e = Engine()
    tok = e.soap_call("HesapServisi", "Login", {"kullaniciAdi": "demo", "sifre": "Demo123!"})["body"]["Token"]
    res = e.soap_call("HesapServisi", "TransferYap", {"token": tok, "kaynakHesapNo": "1000001-1",
                                                      "hedefIban": "TR320010009999901234567890", "tutar": "10.00"})
    assert res["body"]["IslemNo"].startswith("TRX")


def test_allowlist_blocks_address_advertised_by_wsdl(tmp_path, demo_port, monkeypatch):
    # A WSDL that advertises another host (load balancer / machine name) must not be followed silently.
    from service_test_mcp.demo import WSDL

    (tmp_path / "x.wsdl").write_text(WSDL.replace("__ADDRESS__", "http://evil.example/HesapServisi.svc"), "utf-8")
    cfg = write_config(tmp_path, demo_port)
    text = cfg.read_text("utf-8").replace(f"test: http://127.0.0.1:{demo_port}/HesapServisi.svc?wsdl", "test: x.wsdl")
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("STM_CONFIG", str(cfg))
    monkeypatch.setenv("STM_HOME", str(tmp_path / "home"))
    from service_test_mcp.engine import Engine

    e = Engine()
    with pytest.raises(ConfigError, match="allowlist"):
        e.soap_call("HesapServisi", "Login", {"kullaniciAdi": "demo", "sifre": "x"})
    # setting 'endpoint' for the environment fixes it
    cfg.write_text(text.replace("    mutating_operations: [TransferYap]", "    mutating_operations: [TransferYap]\n"
                                f"    endpoint: http://127.0.0.1:{demo_port}/HesapServisi.svc"), encoding="utf-8")
    e = Engine()
    assert e.soap_call("HesapServisi", "Login", {"kullaniciAdi": "demo", "sifre": "x"})["fault"]["code"] == "Client"


def test_rest_calls(engine):
    tok = engine.rest_call("HesapApi", "POST", "/auth/token", body={"username": "demo", "password": "Demo123!"})
    assert tok["status"] == 200
    token = tok["body"]["access_token"]
    det = engine.rest_call("HesapApi", "GET", "/hesaplar/1000001-1", headers={"Authorization": f"Bearer {token}"})
    assert det["status"] == 200 and det["body"]["musteri"]["tckn"] == "10000000146"
    assert "0146" in engine.result_view(det)["body"]["musteri"]["tckn"]
    assert "10000000146" not in json.dumps(engine.result_view(det))
    assert engine.rest_call("HesapApi", "GET", "/hesaplar/1")["status"] == 401
    with pytest.raises(ConfigError, match=r"'\.\.'"):
        engine.rest_call("HesapApi", "GET", "/../admin")
    with pytest.raises(ConfigError, match="base_url"):
        engine.rest_call("HesapApi", "GET", "http://other.example/x")
    with pytest.raises(ConfigError, match="mutating"):
        engine.rest_call("HesapApi", "DELETE", "/hesaplar/1")


def test_audit_log_is_masked(engine, tmp_path):
    engine.soap_call("HesapServisi", "Login", {"kullaniciAdi": "demo", "sifre": "Demo123!"})
    logs = list((tmp_path / "home" / "logs").glob("audit-*.jsonl"))
    assert logs
    line = json.loads(logs[0].read_text("utf-8").splitlines()[-1])
    assert line["service"] == "HesapServisi" and line["operation"] == "Login"
    assert "Demo123!" not in json.dumps(line)


def test_demo_scenarios_pass(engine, demo_config, tmp_path):
    out = Runner(engine).run(str(demo_config.parent / "scenarios"), junit_path=str(tmp_path / "j.xml"))
    assert out["summary"]["scenarios"] == 3 and out["summary"]["failed"] == 0, json.dumps(engine.view(out))[:3000]
    assert 'failures="0"' in (tmp_path / "j.xml").read_text("utf-8")
    tagged = Runner(engine).run(str(demo_config.parent / "scenarios"), tags=["negatif"])
    assert {r["id"] for r in tagged["results"]} == {"negatif-login", "rest-yetkilendirme"}


FAILING = """
id: bilerek-kirik
steps:
  - name: Login
    service: HesapServisi
    operation: Login
    params: { kullaniciAdi: demo, sifre: Demo123! }
    assert:
      - path: $.MusteriNo
        equals: "9999999"
    extract: { token: $.Token }
  - name: Atlanmali
    service: HesapApi
    method: GET
    path: /
"""


def test_failure_report_and_stop(engine):
    out = Runner(engine).run(content=FAILING)
    r = out["results"][0]
    assert not r["passed"]
    step = r["steps"][0]
    assert step["status"] == "failed"
    failed = step["assertions"][0]
    assert failed["actual"] == "1000001" and failed["expected"] == {"equals": "9999999"}
    assert r["steps"][1]["status"] == "skipped"


def test_validate_reports_unknown_service(engine):
    res = Runner(engine).validate(content="id: a\nsteps:\n  - service: Yok\n    operation: X\n")
    assert res["valid"] is False and "unknown service" in res["problems"][0]
