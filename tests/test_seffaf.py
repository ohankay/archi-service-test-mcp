"""Whitebox: tool results carry every HTTP exchange, errors say where they broke, credentials stay labelled."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path

import pytest

from service_test_mcp import iz, server
from service_test_mcp.engine import Engine
from service_test_mcp.masking import Masker


@pytest.fixture()
def seffaf(tanimlar, tmp_path, monkeypatch):
    monkeypatch.setenv("STM_SEFFAF", "seffaf")
    monkeypatch.setenv("STM_IZ_DIR", str(tmp_path / "izler"))
    e = Engine()
    server.set_engine(e)
    yield e
    server.set_engine(None)
    e.close()


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_result_carries_the_whole_flow(seffaf, monkeypatch):
    r = _run(server.run_test("Hesap listeleme", customer="1000001", environment="demo"))
    assert "iz" not in r and "sorun" not in r and len(r["akis"]) == 5   # short by default
    assert list(r)[:2] == ["success", "sonuc"] and "/.../AccountService.svc/AccountListByGroup" in r["akis"][4]
    _run(server.end_sessions())
    monkeypatch.setenv("STM_IZ", "hepsi")
    monkeypatch.setattr(server, "SONUC_LIMIT", 100_000)   # STM_SONUC_SINIR: everything in the result
    r = _run(server.run_test("Hesap listeleme", customer="1000001", environment="demo"))
    assert r["sonuc"] == "geçti"
    assert [line.split("]")[0].split("[")[1] for line in r["akis"]] == \
        ["giriş 1/4", "giriş 2/4", "giriş 3/4", "giriş 4/4", "test"]
    assert "alınan: oturum.SessionKey" in r["akis"][3] and "iş sonucu: başarılı" in r["akis"][4]
    login = r["iz"][1]
    assert login["istek"]["govde"]["CustomerId"] == "1000001"
    assert login["istek"]["basliklar"]["Content-Type"].startswith("application/json")
    assert login["yanit"]["durum"] == 200 and login["yanit"]["govde"]["Success"] is True
    saved = json.loads(Path(r["iz_dosyasi"]).read_text("utf-8"))
    assert len(saved) == 5 and saved[4]["islem"] == "AccountListByGroup"
    # the session is reused: the next call makes one exchange
    r = _run(server.call_operation("Hesap listesi", customer="1000001", environment="demo"))
    assert len(r["akis"]) == 1


def test_credentials_are_labelled_not_hidden_entirely():
    m = Masker(None, mode="seffaf")
    m.add_secret("Cok-Gizli-123", "ozel_sifre", credential=True)
    out = m.mask({"Password": "Cok-Gizli-123", "SessionKey": "abcd1234", "Iban": "TR330006100519786457841326"})
    assert out == {"Password": "«gizli: ozel_sifre»", "SessionKey": "abcd1234",
                   "Iban": "TR330006100519786457841326"}
    assert Masker(None, mode="seffaf", show_secrets=True).mask({"p": "Cok-Gizli-123"}) == {"p": "Cok-Gizli-123"}
    assert "Cok-Gizli-123" not in json.dumps(m.mask({"p": "Cok-Gizli-123"}, kati=True))


def _silent_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    held = []
    threading.Thread(target=lambda: [held.append(sock.accept()) for _ in range(8)], daemon=True).start()
    return sock


def _point_prep_to(tanimlar: Path, address: str, extra: str = "") -> None:
    f = tanimlar / "sistemler" / "mobil-bankacilik.yaml"
    text = f.read_text("utf-8").replace("adres: https://mobileprep.kuveytturk.com.tr",
                                        f"{extra}adres: {address}")
    f.write_text(text, encoding="utf-8")


def test_timeout_says_where_it_broke(tanimlar, tmp_path, monkeypatch):
    sock = _silent_server()
    _point_prep_to(tanimlar, f"http://127.0.0.1:{sock.getsockname()[1]}", "zaman_asimi_sn: 1\n    ")
    for name in ("UYGULAMA_SIFRESI", "UYGULAMA_SIFRESI_2", "MUSTERI_SIFRESI", "OTP"):
        monkeypatch.setenv(f"STM_SECRET_{name}", f"deger-{name}")
    monkeypatch.setenv("STM_SEFFAF", "seffaf")
    monkeypatch.setenv("STM_IZ_DIR", str(tmp_path / "izler"))
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.run_test("Hesap listeleme", customer="1000001", environment="prep"))
    finally:
        server.set_engine(None)
        e.close()
        sock.close()
    mesaj = r["adimlar"][0]["mesaj"]
    assert "giriş 1/4 (LoginForMobileBranch)" in mesaj and "1.0 sn içinde yanıt vermedi" in mesaj
    assert "ReadTimeout" in r["akis"][0]
    hata = r["iz"][0]["hata"]
    assert hata["tur"] == "ReadTimeout" and hata["olasi_nedenler"] and hata["zincir"]
    assert r["iz"][0]["istek"]["zaman_asimi_sn"] == 1.0
    # what was sent is visible; the password is labelled, not printed
    assert r["iz"][0]["istek"]["govde"]["ExtUPassword"] == "«gizli: uygulama_sifresi»"
    assert "deger-UYGULAMA" not in json.dumps(r)


def test_tls_handshake_timeout_is_not_called_a_slow_server(tanimlar, monkeypatch):
    sock = _silent_server()
    _point_prep_to(tanimlar, f"https://127.0.0.1:{sock.getsockname()[1]}", "zaman_asimi_sn: 1\n    proxy: yok\n    ")
    for name in ("UYGULAMA_SIFRESI", "UYGULAMA_SIFRESI_2", "MUSTERI_SIFRESI", "OTP"):
        monkeypatch.setenv(f"STM_SECRET_{name}", f"deger-{name}")
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.run_test("Hesap listeleme", customer="1000001", environment="prep"))
        c = _run(server.check_connection("Mobil Bankacılık", "prep"))
    finally:
        server.set_engine(None)
        e.close()
        sock.close()
    hata = r["iz"][0]["hata"]
    assert "TLS el sıkışması 1.0 sn içinde tamamlanmadı; istek hiç gönderilmedi" in hata["asama"]
    assert any("PAC" in n for n in hata["olasi_nedenler"])
    s = c["sistemler"][0]
    assert [a["durum"] for a in s["yollar"][0]["adimlar"][:3]] == ["tamam", "tamam", "hata"]
    assert "TLS" in s["sonuc"] and any("proxy" in n for n in s["yollar"][0]["adimlar"][2]["olasi_nedenler"])


def test_check_connection_walks_the_steps(seffaf, tanimlar):
    r = _run(server.check_connection(environment="demo"))
    s = r["sistemler"][0]
    assert r["success"] and s["sonuc"] == "erişilebilir"
    assert [a["adim"] for a in s["yollar"][0]["adimlar"]] == ["DNS", "TCP bağlantısı", "HTTP isteği"]
    assert len(json.dumps(r, ensure_ascii=False)) < 4000 and Path(r["ayrinti_dosyasi"]).exists()
    assert s["ayarlar"]["zaman_asimi_sn"] == 30.0


def test_check_connection_refused(tanimlar, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    _point_prep_to(tanimlar, f"http://127.0.0.1:{port}", "proxy: yok\n    ")
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.check_connection("Mobil Bankacılık", "prep"))
    finally:
        server.set_engine(None)
        e.close()
    s = r["sistemler"][0]
    assert not r["success"] and "TCP bağlantısı" in s["sonuc"]
    tcp = s["yollar"][0]["adimlar"][1]
    assert tcp["durum"] == "hata" and tcp["olasi_nedenler"]


def test_unexpected_errors_show_where(seffaf, monkeypatch):
    def boom(*a, **k):
        raise KeyError("beklenmeyen")
    monkeypatch.setattr(server.Kosucu, "kos", boom)
    r = _run(server.run_test("Hesap listeleme", customer="1000001", environment="demo"))
    assert r["success"] is False and r["error"]["type"] == "KeyError"
    assert any("server.py" in k or "test_seffaf.py" in k for k in r["error"]["konum"])


def test_trace_is_scoped_to_one_tool_call():
    with iz.topla() as a:
        iz.ekle({"kaynak": "x"})
        with iz.topla() as b:
            iz.ekle({"kaynak": "y"})
        assert [k["kaynak"] for k in a] == ["x"] and [k["no"] for k in b] == [1]
    assert iz.son() is None


def test_check_connection_tries_direct_when_the_proxy_hangs(tanimlar, demo_port, monkeypatch):
    proxy = _silent_server()
    _point_prep_to(tanimlar, f"http://127.0.0.1:{demo_port}/demo", "zaman_asimi_sn: 1\n    ")
    for key in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    e = Engine()
    server.set_engine(e)
    try:
        r = _run(server.check_connection("Mobil Bankacılık", "prep"))
    finally:
        server.set_engine(None)
        e.close()
        proxy.close()
    s = r["sistemler"][0]
    assert not r["success"] and s["ayarlar"]["proxy"].startswith("http://127.0.0.1:")
    assert [y["calisiyor"] for y in s["yollar"]] == [False, True]
    assert s["yollar"][1]["yol"] == "doğrudan (proxy'siz)" and "proxy: yok" in s["oneri"]


def test_proxy_addresses_from_windows_strings():
    from service_test_mcp.winproxy import proxy_adresleri

    assert proxy_adresleri("ktproxy:8080") == ["http://ktproxy:8080"]
    assert proxy_adresleri("PROXY a.b:81; DIRECT") == ["http://a.b:81"]
    assert proxy_adresleri("http=h1:80;https=h2:8443") == ["http://h1:80", "http://h2:8443"]
    assert proxy_adresleri("DIRECT") == []


def test_saved_connection_settings_apply_without_restart(tanimlar, demo_port, monkeypatch):
    """'proxy: yok' saved by the agent must be used by the next call, not after a restart."""
    proxy = _silent_server()
    _point_prep_to(tanimlar, f"http://127.0.0.1:{demo_port}/demo", "zaman_asimi_sn: 1\n    ")
    for key in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
    from service_test_mcp import demo_mobile as dm

    for name, value in (("UYGULAMA_SIFRESI", dm.APP_PASSWORD), ("UYGULAMA_SIFRESI_2", dm.APP_PASSWORD_2),
                        ("MUSTERI_SIFRESI", dm.CUSTOMER_PASSWORD), ("OTP", dm.OTP)):
        monkeypatch.setenv(f"STM_SECRET_{name}", value)
    e = Engine()
    server.set_engine(e)
    try:
        before = _run(server.call_operation("Kur listesi", customer="1000001", environment="prep"))
        assert before["iz"][0]["istek"]["proxy"].startswith("http://127.0.0.1:"), before
        kayit = e.depo.gerekli("sistem", "Mobil Bankacılık")
        text = kayit.yol.read_text("utf-8").replace("zaman_asimi_sn: 1\n", "zaman_asimi_sn: 1\n    proxy: yok\n")
        r = _run(server.save_definition("sistem", text, version=kayit.surum))
        assert r["success"], r
        after = _run(server.call_operation("Kur listesi", customer="1000001", environment="prep",
                                           checks=["başarılı"]))
        assert e.settings.environments["prep"].proxy == "none"
        assert all("HTTP 200" in line for line in after["akis"]), after["akis"]
    finally:
        server.set_engine(None)
        e.close()
        proxy.close()


def test_failed_login_step_is_the_first_thing_in_the_result(tanimlar, demo_port, monkeypatch, tmp_path):
    from service_test_mcp import demo_mobile as dm

    _point_prep_to(tanimlar, f"http://127.0.0.1:{demo_port}/demo")
    for name, value in (("UYGULAMA_SIFRESI", dm.APP_PASSWORD), ("UYGULAMA_SIFRESI_2", dm.APP_PASSWORD_2),
                        ("MUSTERI_SIFRESI", dm.CUSTOMER_PASSWORD), ("OTP", dm.OTP)):
        monkeypatch.setenv(f"STM_SECRET_{name}", value)
    monkeypatch.setenv("STM_SEFFAF", "seffaf")
    monkeypatch.setenv("STM_IZ_DIR", str(tmp_path / "izler"))
    e = Engine()
    server.set_engine(e)
    try:
        text = asyncio.run(server.run_test("Hesap listeleme", customer="1000002", environment="prep"))
    finally:
        server.set_engine(None)
        e.close()
    r = json.loads(text)
    assert list(r)[:3] == ["success", "sonuc", "sorun"] and r["sonuc"] == "kaldı"
    # the prep user names are not the demo's: the first login step is refused, and that is the first thing said
    assert r["sorun"] == ("giriş 1/4 (LoginForMobileBranch): HTTP 200, iş sonucu başarısız: APP_LOGIN_FAILED; "
                          "Uygulama kullanıcısı doğrulanamadı")
    assert len(r["iz"]) == 1 and "basliklar" not in r["iz"][0]["istek"]
    assert r["iz"][0]["istek"]["govde"]["ExtUPassword"] == "«gizli: uygulama_sifresi»"
    assert len(text) < 8000


def test_long_bodies_keep_their_shape():
    body = {"AccountGroups": [{"Accounts": [{"AccountNumber": 96014828, "AccountName": "Cari Hesap" * 30,
                                             "Balance": str(i)} for i in range(50)]}], "Success": True}
    short = iz._kisalt(body, 1200)
    assert isinstance(short, dict) and short["Success"] is True
    accounts = short["AccountGroups"][0]["Accounts"]
    assert accounts[0]["AccountNumber"] == 96014828 and "öğe" in accounts[-1]
    assert len(json.dumps(short, ensure_ascii=False)) <= 1200


def test_everything_mode_still_fits(seffaf, monkeypatch):
    monkeypatch.setenv("STM_IZ", "hepsi")
    monkeypatch.setattr(server, "SONUC_LIMIT", 3000)
    text = asyncio.run(server.run_test("Hesap listeleme", customer="1000001", environment="demo"))
    r = json.loads(text)
    assert r["sonuc"] == "geçti" and len(r["akis"]) == 5 and Path(r["iz_dosyasi"]).exists()
    assert len(text) < 3500
