"""Definitions: check language, store, runner, generators, log import, migration, default tools, CLI."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from service_test_mcp import demo_mobile
from service_test_mcp.definitions.checks import CheckError, parse, resolve, run_checks
from service_test_mcp.definitions.generators import Generator, all_pairs
from service_test_mcp.definitions.runner import TestRunner
from service_test_mcp.definitions.store import DefinitionError, Store

from .conftest import ROOT, SYSTEM_FILE, clean_env, write_definitions

RESULT = {"body": {"Success": True, "Results": [{"ErrorCode": "E1", "ErrorMessage": "Hata"}],
                   "A": {"L": [{"x": 1, "y": 2, "d": "25.10.2026"}, {"x": 3, "y": 3, "d": "26.10.2026"}]},
                   "Empty": [], "Name": "IŞIK"},
          "business": {"success": True}, "elapsed_ms": 250}
LEGACY = Path(__file__).parent / "legacy_definitions" / "tanimlar"


@pytest.mark.parametrize("line,ok", [
    ("success", True), ("failure", False), ("duration < 1 s", True), ("duration < 100 ms", False),
    ("count A.L = 2", True), ("A.L.x >= 1", True), ("A.L.x > 1", False), ("A.L[1].x = 3", True),
    ("for each A.L: x <= y", True), ("for each A.L: x < y", False),
    ("A.L.d between 24.10.2026 and 27.10.2026", True), ("Name contains ışık", True), ("Name matches ^I", True),
    ("Name does not contain xyz", True), ("error code = E1", True), ("error code contains e", True),
    ("Empty exists", True), ("count Empty = 0", True), ("Nothing missing", True), ("Name is not empty", True),
    ("Empty is empty", True), ("Success = true", True), ("Success = yes", True),
])
def test_check_language(line, ok):
    result = run_checks([line], RESULT, ["Results.ErrorCode"])[0]
    assert result["passed"] is ok, result


@pytest.mark.parametrize("line,why", [("something like this", "is not a check"), ("X between 1", "between"),
                                      ("X matches [", "invalid pattern"), ("X >=", "needs a value")])
def test_check_language_errors(line, why):
    with pytest.raises(CheckError, match=why):
        parse(line)


def test_paths_flatten_lists():
    body = {"G": [{"A": [{"n": 1}, {"n": 2}]}, {"A": [{"n": 3}]}]}
    assert resolve(body, "G.A.n") == [1, 2, 3] and resolve(body, "G[1].A[0].n") == [3] and \
        resolve(body, "G.A") == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_store_validates_and_versions(definitions):
    store = Store.load()
    assert not store.load_errors
    assert store.find("operation", "account transactions").data["method"] == "AccountTransactionHistoryList"
    assert store.find("operation", "GetFXRateList").name == "FX rates"                # also by technical name
    assert store.find("approach", "boundary value").name == "Boundary value analysis"  # by alias
    assert store.find("approach", "sınır değer").name == "Boundary value analysis"     # how users say it
    with pytest.raises(DefinitionError, match="is not defined.*import_log"):
        store.get("operation", "Credit card statement")
    bad = {"name": "x", "steps": [{"operation": "Account transactions", "input": {"nope": 1}, "checks": ["???"]}]}
    problems = store.validate("test", bad)
    assert any("has no input 'nope'" in p for p in problems) and any("is not a check" in p for p in problems)
    assert any("either 'steps'" in p for p in store.validate("test", {
        "name": "x", "operation": "FX rates", "examples": [{"case": "a", "expected": "?"}],
        "steps": [{"operation": "FX rates"}]}))
    new = {"name": "FX pair", "steps": [{"operation": "FX rates",
                                         "checks": ["for each FXRateModelList: CurrencyBid <= CurrencyAsk"]}]}
    record = store.save("test", new)
    assert record.data["status"] == "draft" and record.path.name == "fx-pair.yaml"
    with pytest.raises(DefinitionError, match="already exists"):
        store.save("test", new)
    with pytest.raises(DefinitionError, match="changed after"):
        store.save("test", new, base_version="old")
    assert store.save("test", {**new, "status": "approved"}, base_version=record.version).data["status"] == "approved"
    # hand-written YAML slips are forgiven: "for each X: ..." unquoted is read as a mapping, "field: no" as false
    (definitions / "tests" / "slip.yaml").write_text(
        "name: Slip\nsteps:\n  - operation: FX rates\n    checks:\n"
        "      - for each FXRateModelList: CurrencyBid <= CurrencyAsk\n", encoding="utf-8")
    system = definitions / SYSTEM_FILE
    system.write_text(system.read_text("utf-8").replace('customer: {field: "no"}', "customer: {field: no}"),
                      encoding="utf-8")
    assert not Store.load().load_errors


def test_runs_the_four_flows(definition_engine):
    runner = TestRunner(definition_engine)
    for name in ("Login", "Account listing", "Account transactions view", "FX rate listing"):
        report = runner.run(name, customer="1000001", environment="demo")
        assert report["result"] == "passed", report
    assert demo_mobile.STATS["customer_logins"] == 1                      # one login for all four tests
    report = runner.run("Account transactions view", customer="1000001", environment="demo")
    assert report["steps"][1]["input"]["start"].count(".") == 2           # today-30 -> DD.MM.YYYY
    assert report["ai_review"][0]["question"].startswith("Are the transaction")
    assert "Market" in json.dumps(report["ai_review"][0]["data"], ensure_ascii=False)


def test_failures_observations_and_errors(definition_engine):
    runner = TestRunner(definition_engine)
    test = {"name": "t", "operation": "Account transactions", "examples": [
        {"case": "reversed range", "start": "today", "end": "today-1", "expected": "error",
         "checks": ["error code = INVALID_DATE_RANGE"]},
        {"case": "wrong expectation", "start": "today", "end": "today-1", "expected": "success"},
        {"case": "future", "start": "today", "end": "today+3", "expected": "?"},
        {"case": "unconvertible input", "account_suffix": "one", "expected": "success"}]}
    report = runner.run(content=test, customer="1000001", environment="demo")
    assert [r["result"] for r in report["examples"]] == ["passed", "failed", "observation", "error"]
    assert report["result"] == "failed"
    failed = report["examples"][1]
    assert failed["assertions"][0]["check"] == "success" and failed["assertions"][0]["passed"] is False and \
        "INVALID_DATE_RANGE" in failed["assertions"][0]["actual"]
    assert report["examples"][0]["assertions"][1] == {"check": "error code = INVALID_DATE_RANGE", "source": "example",
                                                      "passed": True, "expected": "an error field = INVALID_DATE_RANGE",
                                                      "actual": ["INVALID_DATE_RANGE",
                                                                 "Bitiş tarihi başlangıçtan önce olamaz"]}
    assert report["examples"][2]["response"] == "success" and "pending_review" in report
    assert "an integer was expected" in report["examples"][3]["message"]
    # a missing required input that has a source is taken from the other operation
    result, effective = runner.call(definition_engine.store.get("operation", "Account transactions"), {}, "1000001",
                                    "demo")
    assert result["business"]["success"] and effective["account_no"] == 1000001
    # without a customer number: the environment's default_customer, and an error when there is none
    assert runner.run("Login", environment="demo")["result"] == "passed"
    definition_engine.profiles["Mobile Banking"].profile.environment("demo").default_customer = None
    report = runner.run("Login", environment="demo")
    assert report["result"] == "failed" and "customer number" in report["steps"][0]["message"]


def test_every_assertion_is_reported(definition_engine):
    """Not only passed/failed: each assertion of each step with where it comes from, what it expected and what came."""
    runner = TestRunner(definition_engine)
    report = runner.run("Account listing", customer="1000001", environment="demo")
    step = report["steps"][0]
    assert [(a["source"], a["check"]) for a in step["assertions"]] == [
        ("system", "success"), ("operation", "AccountGroups exists"),
        ("step", "count AccountGroups.Accounts >= 1"),
        ("step", r"AccountGroups.Accounts.IBANNumber matches ^TR\d{24}$"),
        ("step", "AccountGroups.Accounts.AccountFECCode is not empty"),
        ("step", "for each AccountGroups.Accounts: AccountAvailableBalance <= AccountBalance")]
    assert all(a["passed"] for a in step["assertions"]) and step["assertion_summary"] == \
        {"total": 6, "passed": 6, "failed": 0}
    count = step["assertions"][2]
    assert count["expected"] == "count >= 1" and count["actual"] == 2
    assert step["assertions"][4]["actual"] == ["TRY", "USD"]                # the values seen, not just how many
    # a failing assertion does not hide the passing ones of the same step
    test = {"name": "t", "steps": [{"operation": "Account list", "checks": [
        "count AccountGroups.Accounts >= 1", "AccountGroups.Accounts.AccountFECCode = XYZ"]}]}
    report = runner.run(content=test, customer="1000001", environment="demo")
    records = report["steps"][0]["assertions"]
    assert [a["passed"] for a in records] == [True, True, True, False] and report["result"] == "failed"
    assert records[3]["expected"] == "= XYZ" and records[3]["actual"] == ["TRY", "USD"]
    assert report["assertion_summary"] == {"total": 4, "passed": 3, "failed": 1}
    # templates are shown as they were evaluated
    report = runner.run("Account transactions view", customer="1000001", environment="demo")
    between = next(a for a in report["steps"][1]["assertions"] if "between" in a["check"])
    assert "{{" not in between["check"]
    assert re.search(r"between \d\d\.\d\d\.\d{4} and \d\d\.\d\d\.\d{4}$", between["check"])


def test_customer_is_just_a_number_and_locked(definition_engine, definitions):
    from service_test_mcp.engine import Engine
    from service_test_mcp.profile.runtime import CustomerBusy

    runner = TestRunner(definition_engine)
    assert runner.run("FX rate listing", customer="1000002", environment="demo")["result"] == "passed"
    other = Engine()
    try:
        with pytest.raises(CustomerBusy):
            TestRunner(other).call(other.store.get("operation", "FX rates"), None, "1000002", "demo")
    finally:
        other.close()
    assert demo_mobile.STATS["ended_by_parallel_login"] == 0


def test_generators(definition_engine):
    g = Generator(definition_engine.store)
    bva = g.generate("boundary value", "Account transactions")["test"]
    cases = {r["case"]: r for r in bva["examples"]}
    assert cases["366 days (outside)"]["start"] == "today-366" and cases["366 days (outside)"]["expected"] == "error"
    assert cases["account_suffix: min-1 (outside)"]["account_suffix"] == 0
    assert not definition_engine.store.validate("test", bva)
    assert g.generate("sınır değer", "Account transactions") == g.generate("BVA", "Account transactions")
    report = TestRunner(definition_engine).run(content=bva, customer="1000001", environment="demo")
    assert report["summary"] == {"passed": 7, "observation": 1}, report
    negative = TestRunner(definition_engine).run(content=g.generate("negative", "Account transactions")["test"],
                                                 customer="1000001", environment="demo")
    assert negative["result"] == "passed", negative
    with pytest.raises(DefinitionError, match="pairwise needs"):
        g.generate("pairwise", "Account transactions")
    with pytest.raises(DefinitionError, match="have no limits"):
        g.generate("boundary value", "FX rates")
    agent = g.generate("user journey", "Account list")
    assert agent["generated_by"] == "agent" and agent["rules"]


def test_all_pairs_cover_every_pair():
    values = [["a", "b", "c"], [1, 2, 3], [True, False], ["x", "y"], [0, 1]]
    rows = all_pairs(values)
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
        block("LoginForMobileBranch", auth, "RequestData", {"ExtUPassword": "app-1-secret", "UserId": "u"}),
        block("InitialLogin", auth, "RequestData", {"ExtUPassword": "app-2-secret", "Password": "common-pass",
                                                    "CustomerId": "1000001"}),
        block("OtpLoginCompleteNew", auth, "RequestData", {"GeneratedOtp": "654321", "Password": "common-pass"}),
        block("InsertClientLog", "BOA.Integration.ClientLog/ClientLogService.svc", "RequestData", {"x": 1}),
        block("AccountTransactionHistoryList", "BOA.Integration.MobileBanking.Account/AccountService.svc",
              "RequestData", {"AccountNumber": 1000001, "AccountSuffix": 1, "BeginDate": "21.10.2026",
                              "EndDate": "28.10.2026", "NewFlag": True}),
        block("GetCardList", "BOA.Integration.MobileBanking.Card/CardService.svc", "RequestData",
              {"CardType": "Kredi", "StartDate": "28.09.2026", "CustomerId": "1000001", "ShowAll": True,
               "MainResourceCode": "MOBCARD", "ExtUPassword": "app-2-secret"}),
        block("GetCardList", "BOA.Integration.MobileBanking.Card/CardService.svc", "ResponseData", {"Success": True}),
    ])


def test_log_to_definitions_and_secrets(definition_engine, definitions, demo_port, tmp_path, monkeypatch):
    from service_test_mcp.definitions.log_import import LogImporter

    monkeypatch.setenv("STM_SECRETS_FILE", str(tmp_path / "home" / "secrets.yaml"))
    report = LogImporter(definition_engine).run(content=_log(demo_port))
    text = json.dumps(definition_engine.view(report), ensure_ascii=False)
    assert report["environment"] == "demo" and report["noise_skipped"] == {"InsertClientLog": 1}
    assert [n["method"] for n in report["new_operations"]] == ["GetCardList"]
    card = report["new_operations"][0]
    assert card["inputs"] == {"card_type": "text (default Kredi)", "start_date": "date (default today-30)"}
    assert card["fixed_fields"] == 3                                     # CustomerId, ShowAll, MainResourceCode
    assert report["already_defined"] == [{"operation": "Account transactions",
                                          "method": "AccountTransactionHistoryList",
                                          "fields_not_in_definition": ["NewFlag"]}]
    assert "app-2-secret" not in text and "saved" not in report and "secrets" not in report
    report = LogImporter(definition_engine).run(content=_log(demo_port), apply=True, save_secrets=True)
    draft = yaml.safe_load((definitions / "operations" / "getcardlist.yaml").read_text("utf-8"))
    assert draft["status"] == "draft" and draft["fixed"]["CustomerId"] == "{{customer.no}}"
    assert draft["inputs"]["start_date"] == {"field": "StartDate", "type": "date", "format": "DD.MM.YYYY",
                                             "default": "today-30"}
    assert set(report["secrets"]["saved"]) == {"app_password", "app_password_2", "customer_password", "otp"}
    saved = yaml.safe_load((tmp_path / "home" / "secrets.yaml").read_text("utf-8"))
    assert saved["demo"]["otp"] == "654321"
    for secret in ("app-1-secret", "app-2-secret", "common-pass", "654321"):
        assert secret not in json.dumps(definition_engine.view(report), ensure_ascii=False)
    assert definition_engine.store.find("operation", "GetCardList") is not None   # reloaded


def _legacy_home(tmp_path: Path, port: int) -> Path:
    """A home folder as an earlier version left it: tanimlar/ with Turkish keys, gizli.yaml, a user edit."""
    home = tmp_path / "home"
    shutil.copytree(LEGACY, home / "tanimlar")
    system = home / "tanimlar" / "sistemler" / "mobil-bankacilik.yaml"
    system.write_text(system.read_text("utf-8").replace(":8899", f":{port}").replace(
        "    aciklama: Preprod (mobil uygulamanin prep ortami)\n",
        "    aciklama: Preprod (mobil uygulamanin prep ortami)\n    proxy: yok\n    zaman_asimi_sn: 60\n"),
        encoding="utf-8")
    (home / "gizli.yaml").write_text('prep:\n  uygulama_sifresi: eski-gizli-1\n  otp: "111111"\n', encoding="utf-8")
    return home


def test_migration_of_earlier_definitions(tmp_path, demo_port, monkeypatch):
    """Definitions of the Turkish-named earlier version are converted at the first start and keep the user's edits."""
    from service_test_mcp.engine import Engine

    demo_mobile.reset()
    home = _legacy_home(tmp_path, demo_port)
    for key in ("STM_DEFINITIONS", "STM_SECRETS_FILE", "STM_CONFIG", "STM_LOCKS_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STM_HOME", str(home))
    e = Engine()
    try:
        assert e.migration["migrated"] and not e.store.load_errors and not e.settings.broken, \
            (e.migration, e.store.load_errors, e.settings.broken)
        assert e.settings.environments["prep"].proxy == "none" and e.settings.environments["prep"].timeout_s == 60
        assert e.secrets("prep") == {"app_password": "eski-gizli-1", "otp": "111111"}
        for name in ("Login", "Account listing", "Account transactions view", "FX rate listing"):
            assert TestRunner(e).run(name, customer="1000001", environment="demo")["result"] == "passed", name
    finally:
        e.close()
    # the migrated samples are the new samples (free texts such as descriptions stay as the user wrote them)
    samples = Store.load(ROOT / "src" / "service_test_mcp" / "sample_definitions")
    migrated = Store.load(home / "definitions")
    texts = ("description", "aliases", "when_to_use", "rules", "ai_review")

    def plain(v):
        if isinstance(v, dict):
            return {k: plain(x) for k, x in v.items() if k not in texts and k not in ("proxy", "timeout_s", "address")}
        return [plain(x) for x in v] if isinstance(v, list) else v
    for kind in ("system", "operation", "test", "approach"):
        for record in samples.all(kind):
            other = migrated.find(kind, record.name)
            assert other is not None and plain(other.data) == plain(record.data), (kind, record.name)
    # once: a second start does not overwrite what the user changes afterwards
    edited = home / "definitions" / "tests" / "login.yaml"
    edited.write_text(edited.read_text("utf-8") + "# edited\n", encoding="utf-8")
    Engine().close()
    assert edited.read_text("utf-8").endswith("# edited\n")
    assert (home / "tanimlar").is_dir()                                       # left as a backup


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
    from service_test_mcp.server import BASIC_TOOL_NAMES

    demo_mobile.reset()
    definitions = write_definitions(tmp_path, demo_port)
    env = clean_env(STM_TOOLSET="basic", STM_DEFINITIONS=str(definitions), STM_HOME=str(tmp_path / "home"))

    async def scenario(s):
        tools = (await s.list_tools()).tools
        assert {t.name for t in tools} == set(BASIC_TOOL_NAMES)
        # one copy of each result: no outputSchema / structuredContent (it doubled and escaped every result)
        assert all(t.outputSchema is None for t in tools)
        raw = await s.call_tool("list_tests", {})
        assert raw.structuredContent is None and len(raw.content) == 1
        res = await _call(s, "list_tests")
        assert {t["name"] for t in res["tests"]} == {"Login", "Account listing", "Account transactions view",
                                                     "FX rate listing"}
        assert res["systems"][0]["loaded"] is True and res["default_environment"] == "demo"
        res = await _call(s, "run_test", test="FX rate listing", customer="1000001")
        assert res["result"] == "passed", res
        res = await _call(s, "call_operation", operation="Account transactions", customer="1000001",
                          inputs={"start": "today-30"}, checks=["count AccountTransactionHistoryList >= 3"])
        assert res["business_success"] and res["checks_passed"] and res["input"]["account_no"] == 1000001
        res = await _call(s, "call_operation", operation="Credit card statement", customer="1000001")
        assert res["success"] is False and "import_log" in res["error"]["problems"][0]
        gen = await _call(s, "generate_tests", approach="boundary value", operation="Account transactions")
        assert gen["unknown_expected"] == 1 and "examples" in gen["content"]
        res = await _call(s, "run_test", content=gen["content"], customer="1000001")
        assert res["summary"] == {"passed": 7, "observation": 1}, res
        res = await _call(s, "save_definition", kind="test", content=gen["content"])
        assert res["status"] == "draft"
        res = await _call(s, "save_definition", kind="test", content="name: x\nsteps:\n  - operation: Nope\n")
        assert res["success"] is False and "is not defined" in res["error"]["problems"][0]
        res = await _call(s, "list_tests", kind="test", name="Account transactions - Boundary value analysis")
        assert res["version"] and "366 days" in res["content"]
        res = await _call(s, "run_test", tags=["smoke"], customer="1000001")
        assert res["summary"] == {"passed": 3}, res
        res = await _call(s, "end_sessions", service="archi-service-test")   # the MCP's name means all
        assert res["ended"] and all(x["logged_out"] for x in res["ended"])
    asyncio.run(_session(env, scenario))


def test_cli_init_and_test(tmp_path, demo_port):
    demo_mobile.reset()
    home = tmp_path / "home"
    env = clean_env(STM_HOME=str(home))
    out = subprocess.run([sys.executable, "-m", "service_test_mcp", "--init"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0 and (home / "definitions" / "tests" / "fx-rate-listing.yaml").exists()
    system = home / "definitions" / SYSTEM_FILE
    system.write_text(system.read_text("utf-8").replace(":8899", f":{demo_port}"), encoding="utf-8")
    junit = tmp_path / "j.xml"
    out = subprocess.run([sys.executable, "-m", "service_test_mcp", "--test", "all", "--customer", "1000001",
                          "--junit", str(junit)], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "4/4 tests passed" in out.stdout and junit.exists()
    assert "[PASS] (step) count AccountGroups.Accounts >= 1 - actual: 2" in out.stdout
    cases = junit.read_text("utf-8").count("<testcase ")
    assert cases > 4 and 'name="Account list | count AccountGroups.Accounts &gt;= 1"' in junit.read_text("utf-8")
    again = subprocess.run([sys.executable, "-m", "service_test_mcp", "--init"], env=env, capture_output=True,
                           text=True, timeout=60)
    assert json.loads(again.stdout)["copied"] == []                        # existing files are kept
    schemas = tmp_path / "schemas"
    subprocess.run([sys.executable, "-m", "service_test_mcp", "--schemas", str(schemas)], env=env, check=True,
                   capture_output=True, timeout=60)
    assert {p.name for p in schemas.iterdir()} == {"system.schema.json", "operation.schema.json",
                                                   "test.schema.json", "approach.schema.json"}


def test_cli_migrate(tmp_path, demo_port):
    home = _legacy_home(tmp_path, demo_port)
    out = subprocess.run([sys.executable, "-m", "service_test_mcp", "--migrate"], env=clean_env(STM_HOME=str(home)),
                         capture_output=True, text=True, timeout=60)
    report = json.loads(out.stdout)
    assert out.returncode == 0 and report["migrated"] and len(report["written"]) == 14
    assert (home / "definitions" / SYSTEM_FILE).exists() and (home / "secrets.yaml").exists()
    # a reinstall runs it again: nothing is overwritten without --force
    again = subprocess.run([sys.executable, "-m", "service_test_mcp", "--migrate"], env=clean_env(STM_HOME=str(home)),
                           capture_output=True, text=True, timeout=60)
    assert json.loads(again.stdout)["reason"] == "already migrated"


def test_system_hook(definitions, demo_port):
    from service_test_mcp.engine import Engine

    (definitions / "systems" / "hooks.py").write_text(
        "class Signature:\n"
        "    def __init__(self, profile):\n"
        "        pass\n"
        "    def prepare(self, request, ctx):\n"
        "        request['headers']['X-Operation'] = ctx['operation']\n", encoding="utf-8")
    path = definitions / SYSTEM_FILE
    path.write_text(path.read_text("utf-8") + "hook: hooks.py:Signature\n", encoding="utf-8")
    e = Engine()
    try:
        rt = e.profiles["Mobile Banking"]
        req = rt.build_request(e.settings.environment("demo"), rt.operation("GetFXRateList"), None,
                               {"SessionKey": ""}, {})
        assert req["headers"] == {"X-Operation": "GetFXRateList"}
    finally:
        e.close()
