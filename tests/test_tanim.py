"""Definitions (tanimlar): check language, store, runner, generators, log import, default tools, CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest
import yaml

from service_test_mcp import demo_mobile
from service_test_mcp.tanim.depo import Depo, TanimHatasi
from service_test_mcp.tanim.kontrol import KontrolHatasi, ayristir, coz, kontrol_et
from service_test_mcp.tanim.kosucu import Kosucu
from service_test_mcp.tanim.uretec import Uretec, tum_ciftler

from .conftest import clean_env, write_tanimlar

SONUC = {"body": {"Success": True, "Results": [{"ErrorCode": "E1", "ErrorMessage": "Hata"}],
                  "A": {"L": [{"x": 1, "y": 2, "d": "25.10.2026"}, {"x": 3, "y": 3, "d": "26.10.2026"}]},
                  "Bos": [], "Ad": "IŞIK"},
         "business": {"success": True}, "elapsed_ms": 250}


@pytest.mark.parametrize("line,ok", [
    ("başarılı", True), ("başarısız", False), ("süre < 1 sn", True), ("süre < 100 ms", False),
    ("A.L sayısı = 2", True), ("A.L.x >= 1", True), ("A.L.x > 1", False), ("A.L[1].x = 3", True),
    ("her A.L için x <= y", True), ("her A.L için x < y", False), ("her A.L: x <= y", True),
    ("A.L.d arasında 24.10.2026 ve 27.10.2026", True), ("Ad içerir ışık", True), ("Ad eşleşir ^I", True),
    ("hata kodu = E1", True), ("hata kodu içerir e", True), ("Bos var", True), ("Bos sayısı = 0", True),
    ("Yok yok", True), ("Ad boş değil", True), ("Success = true", True), ("Success = evet", True),
])
def test_check_language(line, ok):
    result = kontrol_et([line], SONUC, ["Results.ErrorCode"])[0]
    assert result["gecti"] is ok, result


@pytest.mark.parametrize("line,why", [("böyle bir şey", "anlaşılamadı"), ("X arasında 1", "arasında"),
                                      ("X eşleşir [", "geçersiz kalıp"), ("X >=", "değer gerekli")])
def test_check_language_errors(line, why):
    with pytest.raises(KontrolHatasi, match=why):
        ayristir(line)


def test_paths_flatten_lists():
    body = {"G": [{"A": [{"n": 1}, {"n": 2}]}, {"A": [{"n": 3}]}]}
    assert coz(body, "G.A.n") == [1, 2, 3] and coz(body, "G[1].A[0].n") == [3] and coz(body, "G.A") == \
        [{"n": 1}, {"n": 2}, {"n": 3}]


def test_store_validates_and_versions(tanimlar):
    depo = Depo.yukle()
    assert not depo.yukleme_hatalari
    assert depo.bul("islem", "hesap hareketleri").veri["metod"] == "AccountTransactionHistoryList"
    assert depo.bul("islem", "GetFXRateList").ad == "Kur listesi"            # also by technical name
    assert depo.bul("yaklasim", "boundary value").ad == "Sınır değer analizi"  # by alias
    with pytest.raises(TanimHatasi, match="tanımlı değil.*import_log"):
        depo.gerekli("islem", "Kredi kartı ekstresi")
    bad = {"test": "x", "adimlar": [{"islem": "Hesap hareketleri", "girdi": {"yok": 1}, "kontrol": ["???"]}]}
    problems = depo.dogrula("test", bad)
    assert any("'yok' girdisi yok" in p for p in problems) and any("anlaşılamadı" in p for p in problems)
    assert any("ya 'adimlar'" in p for p in depo.dogrula("test", {"test": "x", "islem": "Kur listesi",
                                                                  "ornekler": [{"durum": "a", "beklenen": "?"}],
                                                                  "adimlar": [{"islem": "Kur listesi"}]}))
    yeni = {"test": "Kur çift", "adimlar": [{"islem": "Kur listesi", "kontrol": ["her FXRateModelList: "
                                                                                 "CurrencyBid <= CurrencyAsk"]}]}
    kayit = depo.kaydet("test", yeni)
    assert kayit.veri["durum"] == "taslak" and kayit.yol.name == "kur-cift.yaml"
    with pytest.raises(TanimHatasi, match="zaten var"):
        depo.kaydet("test", yeni)
    with pytest.raises(TanimHatasi, match="değişti"):
        depo.kaydet("test", yeni, temel_surum="eski")
    assert depo.kaydet("test", {**yeni, "durum": "onayli"}, temel_surum=kayit.surum).veri["durum"] == "onayli"
    # hand-written YAML slips are forgiven: "her X: ..." read as a mapping, "alan: no" read as false
    text = (tanimlar / "testler" / "kur-listesi.yaml").read_text("utf-8").replace(" için ", ": ")
    (tanimlar / "testler" / "kur-listesi.yaml").write_text(text, encoding="utf-8")
    assert not Depo.yukle().yukleme_hatalari


def test_runs_the_four_flows(tanim_engine):
    k = Kosucu(tanim_engine)
    for name in ("Giriş", "Hesap listeleme", "Hesap hareketleri görüntüleme", "Kur bilgisi listeleme"):
        rapor = k.kos(name, musteri="1000001", ortam="demo")
        assert rapor["sonuc"] == "geçti", rapor
    assert demo_mobile.STATS["customer_logins"] == 1                      # one login for all four tests
    rapor = k.kos("Hesap hareketleri görüntüleme", musteri="1000001", ortam="demo")
    assert rapor["adimlar"][1]["girdi"]["baslangic"].count(".") == 2       # bugün-30 -> GG.AA.YYYY
    assert rapor["ai_yorum"][0]["soru"].startswith("Hareket açıklamaları")
    assert "Market" in json.dumps(rapor["ai_yorum"][0]["veri"], ensure_ascii=False)


def test_failures_observations_and_errors(tanim_engine):
    k = Kosucu(tanim_engine)
    test = {"test": "t", "islem": "Hesap hareketleri", "ornekler": [
        {"durum": "ters aralık", "baslangic": "bugün", "bitis": "bugün-1", "beklenen": "hata",
         "kontrol": ["hata kodu = INVALID_DATE_RANGE"]},
        {"durum": "yanlış beklenti", "baslangic": "bugün", "bitis": "bugün-1", "beklenen": "başarılı"},
        {"durum": "gelecek", "baslangic": "bugün", "bitis": "bugün+3", "beklenen": "?"},
        {"durum": "çevrilemeyen girdi", "hesap_ek": "bir", "beklenen": "başarılı"}]}
    rapor = k.kos(icerik=test, musteri="1000001", ortam="demo")
    sonuc = [r["sonuc"] for r in rapor["satirlar"]]
    assert sonuc == ["geçti", "kaldı", "gözlem", "hata"] and rapor["sonuc"] == "kaldı"
    kaldi = rapor["satirlar"][1]
    assert kaldi["kalan_kontroller"][0]["kontrol"] == "başarılı" and "INVALID_DATE_RANGE" in \
        kaldi["kalan_kontroller"][0]["gercek"]
    assert rapor["satirlar"][2]["cevap"] == "başarılı" and "onay_bekleyen" in rapor
    assert "sayı bekleniyordu" in rapor["satirlar"][3]["mesaj"]
    # a missing required input that has a source is taken from the other operation
    sonuc, etkin = k.cagir(tanim_engine.depo.gerekli("islem", "Hesap hareketleri"), {}, "1000001", "demo")
    assert sonuc["business"]["success"] and etkin["hesap_no"] == 1000001
    # without a customer number: the environment's varsayilan_musteri, and an error when there is none
    assert k.kos("Giriş", ortam="demo")["sonuc"] == "geçti"
    tanim_engine.profiles["Mobil Bankacılık"].profile.environment("demo").default_customer = None
    rapor = k.kos("Giriş", ortam="demo")
    assert rapor["sonuc"] == "kaldı" and "müşteri numarası" in rapor["adimlar"][0]["mesaj"]


def test_customer_is_just_a_number_and_locked(tanim_engine, tanimlar):
    from service_test_mcp.engine import Engine
    from service_test_mcp.profile.runtime import CustomerBusy

    k = Kosucu(tanim_engine)
    assert k.kos("Kur bilgisi listeleme", musteri="1000002", ortam="demo")["sonuc"] == "geçti"
    other = Engine()
    try:
        with pytest.raises(CustomerBusy):
            Kosucu(other).cagir(other.depo.gerekli("islem", "Kur listesi"), None, "1000002", "demo")
    finally:
        other.close()
    assert demo_mobile.STATS["ended_by_parallel_login"] == 0


def test_generators(tanim_engine):
    u = Uretec(tanim_engine.depo)
    bva = u.uret("sınır değer", "Hesap hareketleri")["test"]
    durumlar = {r["durum"]: r for r in bva["ornekler"]}
    assert durumlar["366 gün (sınır dışı)"]["baslangic"] == "bugün-366" and \
        durumlar["366 gün (sınır dışı)"]["beklenen"] == "hata"
    assert durumlar["hesap_ek: en az-1 (sınır dışı)"]["hesap_ek"] == 0
    assert not tanim_engine.depo.dogrula("test", bva)
    assert u.uret("sınır değer", "Hesap hareketleri") == u.uret("sınır değer", "Hesap hareketleri")  # deterministic
    rapor = Kosucu(tanim_engine).kos(icerik=bva, musteri="1000001", ortam="demo")
    assert rapor["ozet"] == {"geçti": 7, "gözlem": 1}, rapor
    neg = Kosucu(tanim_engine).kos(icerik=u.uret("negatif", "Hesap hareketleri")["test"], musteri="1000001",
                                   ortam="demo")
    assert neg["sonuc"] == "geçti", neg
    with pytest.raises(TanimHatasi, match="pairwise için"):
        u.uret("pairwise", "Hesap hareketleri")
    with pytest.raises(TanimHatasi, match="sınır yok"):
        u.uret("sınır değer", "Kur listesi")
    ajan = u.uret("kullanıcı yolculuğu", "Hesap listesi")
    assert ajan["uretim"] == "ajan" and ajan["kurallar"]


def test_all_pairs_cover_every_pair():
    values = [["a", "b", "c"], [1, 2, 3], [True, False], ["x", "y"], [0, 1]]
    rows = tum_ciftler(values)
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            assert {(r[i], r[j]) for r in rows} == {(a, b) for a in values[i] for b in values[j]}
    assert len(rows) < 3 * 3 * 2 * 2 * 2


def _log(port: int) -> str:
    base = f"http://127.0.0.1:{port}/demo/BOA.Integration.MobileBranch"

    def block(name: str, path: str, kind: str, data: dict) -> str:
        head = "Method: HTTPMethod(rawValue: \"POST\")\n" if kind == "RequestData" else ""
        return (f"{'*' * 65}\nDate: 28-10-2026 14:07:22\n{head}Method Name: {name}\nURL: {base}/{path}/{name}\n"
                f"{kind}: {json.dumps(data)}\n")
    auth = "BOA.Integration.Authentication/AuthenticationService.svc"
    return "".join([
        block("LoginForMobileBranch", auth, "RequestData", {"ExtUPassword": "uyg-1-gizli", "UserId": "u"}),
        block("InitialLogin", auth, "RequestData", {"ExtUPassword": "uyg-2-gizli", "Password": "ortak-sifre",
                                                    "CustomerId": "1000001"}),
        block("OtpLoginCompleteNew", auth, "RequestData", {"GeneratedOtp": "654321", "Password": "ortak-sifre"}),
        block("InsertClientLog", "BOA.Integration.ClientLog/ClientLogService.svc", "RequestData", {"x": 1}),
        block("AccountTransactionHistoryList", "BOA.Integration.MobileBanking.Account/AccountService.svc",
              "RequestData", {"AccountNumber": 1000001, "AccountSuffix": 1, "BeginDate": "21.10.2026",
                              "EndDate": "28.10.2026", "NewFlag": True}),
        block("GetCardList", "BOA.Integration.MobileBanking.Card/CardService.svc", "RequestData",
              {"CardType": "Kredi", "StartDate": "28.09.2026", "CustomerId": "1000001", "ShowAll": True,
               "MainResourceCode": "MOBCARD", "ExtUPassword": "uyg-2-gizli"}),
        block("GetCardList", "BOA.Integration.MobileBanking.Card/CardService.svc", "ResponseData", {"Success": True}),
    ])


def test_log_to_definitions_and_secrets(tanim_engine, tanimlar, demo_port, tmp_path, monkeypatch):
    from service_test_mcp.tanim.logdan import LogdanTanim

    monkeypatch.setenv("STM_GIZLI", str(tmp_path / "home" / "gizli.yaml"))
    rapor = LogdanTanim(tanim_engine).calistir(content=_log(demo_port))
    text = json.dumps(tanim_engine.view(rapor), ensure_ascii=False)
    assert rapor["ortam"] == "demo" and rapor["gurultu_atlanan"] == {"InsertClientLog": 1}
    assert [y["metod"] for y in rapor["yeni_islemler"]] == ["GetCardList"]
    kart = rapor["yeni_islemler"][0]
    assert kart["girdiler"] == {"card_type": "metin (varsayılan Kredi)", "start_date": "tarih (varsayılan bugün-30)"}
    assert kart["sabit_alan_sayisi"] == 3                                # CustomerId, ShowAll, MainResourceCode
    assert rapor["zaten_tanimli"] == [{"islem": "Hesap hareketleri", "metod": "AccountTransactionHistoryList",
                                       "tanimda_olmayan_alanlar": ["NewFlag"]}]
    assert "uyg-2-gizli" not in text and "kaydedilen" not in rapor and "gizli" not in rapor
    rapor = LogdanTanim(tanim_engine).calistir(content=_log(demo_port), apply=True, save_secrets=True)
    taslak = yaml.safe_load((tanimlar / "islemler" / "getcardlist.yaml").read_text("utf-8"))
    assert taslak["durum"] == "taslak" and taslak["sabit"]["CustomerId"] == "{{musteri.no}}"
    assert taslak["girdiler"]["start_date"] == {"alan": "StartDate", "tip": "tarih", "bicim": "GG.AA.YYYY",
                                                "varsayilan": "bugün-30"}
    assert set(rapor["gizli"]["kaydedilen"]) == {"uygulama_sifresi", "uygulama_sifresi_2", "musteri_sifresi", "otp"}
    saved = yaml.safe_load((tmp_path / "home" / "gizli.yaml").read_text("utf-8"))
    assert saved["demo"]["otp"] == "654321"
    for secret in ("uyg-1-gizli", "uyg-2-gizli", "ortak-sifre", "654321"):
        assert secret not in json.dumps(tanim_engine.view(rapor), ensure_ascii=False)
    assert tanim_engine.depo.bul("islem", "GetCardList") is not None   # reloaded


async def _session(env, fn):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "service_test_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def _call(session, _tool, **args):
    res = await session.call_tool(_tool, args)
    return json.loads(res.content[0].text)


def test_default_tools_over_stdio(tmp_path, demo_port):
    from service_test_mcp.server import TEMEL_TOOL_NAMES

    demo_mobile.reset()
    tanimlar = write_tanimlar(tmp_path, demo_port)
    env = clean_env(STM_TOOLSET="temel", STM_TANIMLAR=str(tanimlar), STM_HOME=str(tmp_path / "home"))

    async def scenario(s):
        assert {t.name for t in (await s.list_tools()).tools} == set(TEMEL_TOOL_NAMES)
        res = await _call(s, "list_tests")
        assert {t["test"] for t in res["testler"]} == {"Giriş", "Hesap listeleme", "Hesap hareketleri görüntüleme",
                                                       "Kur bilgisi listeleme"}
        assert res["sistemler"][0]["calisiyor"] is True and res["varsayilan_ortam"] == "demo"
        res = await _call(s, "run_test", test="Kur bilgisi listeleme", customer="1000001")
        assert res["sonuc"] == "geçti", res
        res = await _call(s, "call_operation", operation="Hesap hareketleri", customer="1000001",
                          inputs={"baslangic": "bugün-30"}, checks=["AccountTransactionHistoryList sayısı >= 3"])
        assert res["basarili"] and res["kontroller_gecti"] and res["girdi"]["hesap_no"] == 1000001
        res = await _call(s, "call_operation", operation="Kredi kartı ekstresi", customer="1000001")
        assert res["success"] is False and "import_log" in res["error"]["problems"][0]
        gen = await _call(s, "generate_tests", approach="boundary value", operation="Hesap hareketleri")
        assert gen["bilinmeyen_beklenen"] == 1 and "ornekler" in gen["icerik"]
        res = await _call(s, "run_test", content=gen["icerik"], customer="1000001")
        assert res["ozet"] == {"geçti": 7, "gözlem": 1}, res
        res = await _call(s, "save_definition", kind="test", content=gen["icerik"])
        assert res["durum"] == "taslak"
        res = await _call(s, "save_definition", kind="test", content="test: x\nadimlar:\n  - islem: Yok\n")
        assert res["success"] is False and "tanımlı değil" in res["error"]["problems"][0]
        res = await _call(s, "list_tests", kind="test", name="Hesap hareketleri - Sınır değer analizi")
        assert res["surum"] and "366 gün" in res["icerik"]
        res = await _call(s, "run_test", tags=["duman"], customer="1000001")
        assert res["ozet"] == {"geçti": 3}, res
        res = await _call(s, "end_sessions")
        assert res["ended"] and all(x["logged_out"] for x in res["ended"])
    asyncio.run(_session(env, scenario))


def test_cli_init_and_test(tmp_path, demo_port):
    demo_mobile.reset()
    home = tmp_path / "home"
    env = clean_env(STM_HOME=str(home))
    out = subprocess.run([sys.executable, "-m", "service_test_mcp", "--init"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0 and (home / "tanimlar" / "testler" / "kur-listesi.yaml").exists()
    system = home / "tanimlar" / "sistemler" / "mobil-bankacilik.yaml"
    system.write_text(system.read_text("utf-8").replace(":8899", f":{demo_port}"), encoding="utf-8")
    junit = tmp_path / "j.xml"
    out = subprocess.run([sys.executable, "-m", "service_test_mcp", "--test", "hepsi", "--musteri", "1000001",
                          "--junit", str(junit)], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "4/4 test geçti" in out.stdout and junit.exists()
    again = subprocess.run([sys.executable, "-m", "service_test_mcp", "--init"], env=env, capture_output=True,
                           text=True, timeout=60)
    assert json.loads(again.stdout)["kopyalanan"] == []                    # existing files are kept
    schemas = tmp_path / "semalar"
    subprocess.run([sys.executable, "-m", "service_test_mcp", "--schemas", str(schemas)], env=env, check=True,
                   capture_output=True, timeout=60)
    assert {p.name for p in schemas.iterdir()} == {"sistem.schema.json", "islem.schema.json", "test.schema.json",
                                                   "yaklasim.schema.json"}


def test_system_hook(tanimlar, demo_port):
    from service_test_mcp.engine import Engine

    (tanimlar / "sistemler" / "kancalar.py").write_text(
        "class Imza:\n"
        "    def __init__(self, profile):\n"
        "        pass\n"
        "    def prepare(self, request, ctx):\n"
        "        request['headers']['X-Islem'] = ctx['operation']\n", encoding="utf-8")
    path = tanimlar / "sistemler" / "mobil-bankacilik.yaml"
    path.write_text(path.read_text("utf-8") + "kanca: kancalar.py:Imza\n", encoding="utf-8")
    e = Engine()
    try:
        rt = e.profiles["Mobil Bankacılık"]
        req = rt.build_request(e.settings.environment("demo"), rt.operation("GetFXRateList"), None,
                               {"SessionKey": ""}, {})
        assert req["headers"] == {"X-Islem": "GetFXRateList"}
    finally:
        e.close()
