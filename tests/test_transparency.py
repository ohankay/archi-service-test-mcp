"""Whitebox: tool results carry every HTTP exchange, errors say where they broke, credentials stay labelled."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path

import pytest

from service_test_mcp import demo_mobile as dm
from service_test_mcp import server, trace
from service_test_mcp.engine import Engine
from service_test_mcp.masking import Masker

from .conftest import SYSTEM_FILE

SECRETS = (("APP_PASSWORD", dm.APP_PASSWORD), ("APP_PASSWORD_2", dm.APP_PASSWORD_2),
           ("CUSTOMER_PASSWORD", dm.CUSTOMER_PASSWORD), ("OTP", dm.OTP))


@pytest.fixture()
def transparent(definitions, tmp_path, monkeypatch):
    monkeypatch.setenv("STM_TRANSPARENCY", "transparent")
    monkeypatch.setenv("STM_TRACE_DIR", str(tmp_path / "traces"))
    e = Engine()
    server.set_engine(e)
    yield e
    server.set_engine(None)
    e.close()


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_result_carries_the_whole_flow(transparent, monkeypatch):
    r = _run(server.run_test("Account listing", customer="1000001", environment="demo"))
    assert "trace" not in r and "problem" not in r and len(r["flow"]) == 5   # short by default
    assert list(r)[:2] == ["success", "result"] and "/.../AccountService.svc/AccountListByGroup" in r["flow"][4]
    _run(server.end_sessions())
    monkeypatch.setenv("STM_TRACE", "all")
    monkeypatch.setattr(server, "RESULT_LIMIT", 100_000)   # STM_RESULT_CHARS: everything in the result
    r = _run(server.run_test("Account listing", customer="1000001", environment="demo"))
    assert r["result"] == "passed"
    assert [line.split("]")[0].split("[")[1] for line in r["flow"]] == \
        ["login 1/4", "login 2/4", "login 3/4", "login 4/4", "test"]
    assert "extracted: session.SessionKey" in r["flow"][3] and "business result: success" in r["flow"][4]
    login = r["trace"][1]
    assert login["request"]["body"]["CustomerId"] == "1000001"
    assert login["request"]["headers"]["Content-Type"].startswith("application/json")
    assert login["response"]["status"] == 200 and login["response"]["body"]["Success"] is True
    saved = json.loads(Path(r["trace_file"]).read_text("utf-8"))
    assert len(saved) == 5 and saved[4]["operation"] == "AccountListByGroup"
    # the session is reused: the next call makes one exchange
    r = _run(server.call_operation("Account list", customer="1000001", environment="demo"))
    assert len(r["flow"]) == 1


def test_credentials_are_labelled_not_hidden_entirely():
    m = Masker(None, mode="transparent")
    m.add_secret("Very-Secret-123", "own_password", credential=True)
    out = m.mask({"Password": "Very-Secret-123", "SessionKey": "abcd1234", "Iban": "TR330006100519786457841326"})
    assert out == {"Password": "«secret: own_password»", "SessionKey": "abcd1234",
                   "Iban": "TR330006100519786457841326"}
    assert Masker(None, mode="transparent", show_secrets=True).mask({"p": "Very-Secret-123"}) == \
        {"p": "Very-Secret-123"}
    assert "Very-Secret-123" not in json.dumps(m.mask({"p": "Very-Secret-123"}, strict=True))


def test_earlier_setting_names_still_work(monkeypatch):
    from service_test_mcp.masking import mode_from_env

    monkeypatch.delenv("STM_TRANSPARENCY", raising=False)
    monkeypatch.setenv("STM_SEFFAF", "maskeli")             # an mcp.json written for an earlier version
    assert mode_from_env() == "masked"
    assert server.toolset_name("uzman") == "expert" and server.toolset_name("temel") == "basic"


def _silent_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    held = []
    threading.Thread(target=lambda: [held.append(sock.accept()) for _ in range(8)], daemon=True).start()
    return sock


def _point_prep_to(definitions: Path, address: str, extra: str = "") -> None:
    f = definitions / SYSTEM_FILE
    text = f.read_text("utf-8").replace("address: https://mobileprep.kuveytturk.com.tr", f"{extra}address: {address}")
    f.write_text(text, encoding="utf-8")


def test_timeout_says_where_it_broke(definitions, tmp_path, monkeypatch):
    sock = _silent_server()
    _point_prep_to(definitions, f"http://127.0.0.1:{sock.getsockname()[1]}", "timeout_s: 1\n    ")
    for name, _ in SECRETS:
        monkeypatch.setenv(f"STM_SECRET_{name}", f"value-{name}")
    monkeypatch.setenv("STM_TRANSPARENCY", "transparent")
    monkeypatch.setenv("STM_TRACE_DIR", str(tmp_path / "traces"))
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.run_test("Account listing", customer="1000001", environment="prep"))
    finally:
        server.set_engine(None)
        e.close()
        sock.close()
    message = r["steps"][0]["message"]
    assert "login 1/4 (LoginForMobileBranch)" in message and "did not answer in 1.0 s" in message
    assert "ReadTimeout" in r["flow"][0]
    error = r["trace"][0]["error"]
    assert error["kind"] == "ReadTimeout" and error["likely_causes"] and error["chain"]
    assert r["trace"][0]["request"]["timeout_s"] == 1.0
    # what was sent is visible; the password is labelled, not printed
    assert r["trace"][0]["request"]["body"]["ExtUPassword"] == "«secret: app_password»"
    assert "value-APP" not in json.dumps(r)


def test_tls_handshake_timeout_is_not_called_a_slow_server(definitions, monkeypatch):
    sock = _silent_server()
    _point_prep_to(definitions, f"https://127.0.0.1:{sock.getsockname()[1]}", "timeout_s: 1\n    proxy: none\n    ")
    for name, _ in SECRETS:
        monkeypatch.setenv(f"STM_SECRET_{name}", f"value-{name}")
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.run_test("Account listing", customer="1000001", environment="prep"))
        c = _run(server.check_connection("Mobile Banking", "prep"))
    finally:
        server.set_engine(None)
        e.close()
        sock.close()
    error = r["trace"][0]["error"]
    assert "the TLS handshake did not complete in 1.0 s; the request was never sent" in error["phase"]
    assert any("PAC" in n for n in error["likely_causes"])
    s = c["systems"][0]
    assert [a["status"] for a in s["routes"][0]["steps"][:3]] == ["ok", "ok", "error"]
    assert "TLS" in s["result"] and any("proxy" in n for n in s["routes"][0]["steps"][2]["likely_causes"])


def test_check_connection_walks_the_steps(transparent, definitions):
    r = _run(server.check_connection(environment="demo"))
    s = r["systems"][0]
    assert r["success"] and s["result"] == "reachable"
    assert [a["step"] for a in s["routes"][0]["steps"]] == ["DNS", "TCP connection", "HTTP request"]
    assert len(json.dumps(r, ensure_ascii=False)) < 4000 and Path(r["details_file"]).exists()
    assert s["settings"]["timeout_s"] == 30.0


def test_check_connection_refused(definitions, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    _point_prep_to(definitions, f"http://127.0.0.1:{port}", "proxy: none\n    ")
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.check_connection("Mobile Banking", "prep"))
    finally:
        server.set_engine(None)
        e.close()
    s = r["systems"][0]
    assert not r["success"] and "TCP connection" in s["result"]
    tcp = s["routes"][0]["steps"][1]
    assert tcp["status"] == "error" and tcp["likely_causes"]


def test_unexpected_errors_show_where(transparent, monkeypatch):
    def boom(*a, **k):
        raise KeyError("unexpected")
    monkeypatch.setattr(server.TestRunner, "run", boom)
    r = _run(server.run_test("Account listing", customer="1000001", environment="demo"))
    assert r["success"] is False and r["error"]["type"] == "KeyError"
    assert any("server.py" in k or "test_transparency.py" in k for k in r["error"]["location"])


def test_trace_is_scoped_to_one_tool_call():
    with trace.collect() as a:
        trace.add({"origin": "x"})
        with trace.collect() as b:
            trace.add({"origin": "y"})
        assert [r["origin"] for r in a] == ["x"] and [r["no"] for r in b] == [1]
    assert trace.last() is None


def test_check_connection_tries_direct_when_the_proxy_hangs(definitions, demo_port, monkeypatch):
    proxy = _silent_server()
    _point_prep_to(definitions, f"http://127.0.0.1:{demo_port}/demo", "timeout_s: 1\n    ")
    for key in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.check_connection("Mobile Banking", "prep"))
    finally:
        server.set_engine(None)
        e.close()
        proxy.close()
    s = r["systems"][0]
    assert not r["success"] and s["settings"]["proxy"].startswith("http://127.0.0.1:")
    assert [route["works"] for route in s["routes"]] == [False, True]
    assert s["routes"][1]["route"] == "direct (no proxy)" and "proxy: none" in s["suggestion"]


def test_proxy_addresses_from_windows_strings():
    from service_test_mcp.winproxy import proxy_urls

    assert proxy_urls("corpproxy:8080") == ["http://corpproxy:8080"]
    assert proxy_urls("PROXY a.b:81; DIRECT") == ["http://a.b:81"]
    assert proxy_urls("http=h1:80;https=h2:8443") == ["http://h1:80", "http://h2:8443"]
    assert proxy_urls("DIRECT") == []


def test_saved_connection_settings_apply_without_restart(definitions, demo_port, monkeypatch):
    """'proxy: none' saved by the agent must be used by the next call, not after a restart."""
    proxy = _silent_server()
    _point_prep_to(definitions, f"http://127.0.0.1:{demo_port}/demo", "timeout_s: 1\n    ")
    for key in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    for name, value in SECRETS:
        monkeypatch.setenv(f"STM_SECRET_{name}", value)
    e = Engine()
    server.set_engine(e)
    try:
        before = _run(server.call_operation("FX rates", customer="1000001", environment="prep"))
        assert before["trace"][0]["request"]["proxy"].startswith("http://127.0.0.1:"), before
        record = e.store.get("system", "Mobile Banking")
        text = record.path.read_text("utf-8").replace("timeout_s: 1\n", "timeout_s: 1\n    proxy: none\n")
        r = _run(server.save_definition("system", text, version=record.version))
        assert r["success"], r
        after = _run(server.call_operation("FX rates", customer="1000001", environment="prep", checks=["success"]))
        assert e.settings.environments["prep"].proxy == "none"
        assert all("HTTP 200" in line for line in after["flow"]), after["flow"]
    finally:
        server.set_engine(None)
        e.close()
        proxy.close()


def test_failed_login_step_is_the_first_thing_in_the_result(definitions, demo_port, monkeypatch, tmp_path):
    _point_prep_to(definitions, f"http://127.0.0.1:{demo_port}/demo")
    for name, value in SECRETS:
        monkeypatch.setenv(f"STM_SECRET_{name}", value)
    monkeypatch.setenv("STM_TRANSPARENCY", "transparent")
    monkeypatch.setenv("STM_TRACE_DIR", str(tmp_path / "traces"))
    e = Engine()
    server.set_engine(e)
    try:
        text = asyncio.run(server.run_test("Account listing", customer="1000002", environment="prep"))
    finally:
        server.set_engine(None)
        e.close()
    r = json.loads(text)
    assert list(r)[:3] == ["success", "result", "problem"] and r["result"] == "failed"
    # the prep user names are not the demo's: the first login step is refused, and that is the first thing said
    # (the error text is the service's own message)
    assert r["problem"] == ("login 1/4 (LoginForMobileBranch): HTTP 200, business result failure: APP_LOGIN_FAILED; "
                            "Uygulama kullanıcısı doğrulanamadı")
    assert len(r["trace"]) == 1 and "headers" not in r["trace"][0]["request"]
    assert r["trace"][0]["request"]["body"]["ExtUPassword"] == "«secret: app_password»"
    assert len(text) < 8000


def test_long_bodies_keep_their_shape():
    body = {"AccountGroups": [{"Accounts": [{"AccountNumber": 96014828, "AccountName": "Cari Hesap" * 30,
                                             "Balance": str(i)} for i in range(50)]}], "Success": True}
    short = trace.shorten(body, 1200)
    assert isinstance(short, dict) and short["Success"] is True
    accounts = short["AccountGroups"][0]["Accounts"]
    assert accounts[0]["AccountNumber"] == 96014828 and "items" in accounts[-1]
    assert len(json.dumps(short, ensure_ascii=False)) <= 1200


def test_everything_mode_still_fits(transparent, monkeypatch):
    monkeypatch.setenv("STM_TRACE", "all")
    monkeypatch.setattr(server, "RESULT_LIMIT", 3000)
    text = asyncio.run(server.run_test("Account listing", customer="1000001", environment="demo"))
    r = json.loads(text)
    assert r["result"] == "passed" and len(r["flow"]) == 5 and Path(r["trace_file"]).exists()
    assert len(text) < 3500
