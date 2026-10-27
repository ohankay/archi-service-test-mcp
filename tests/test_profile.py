"""Profile services against the in-process mobile WCF demo: login, session, test_customers, leases, scenarios."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from service_test_mcp import demo_mobile
from service_test_mcp.config import ConfigError
from service_test_mcp.profile.leases import FileLeaseStore
from service_test_mcp.profile.model import Profile
from service_test_mcp.profile.runtime import CustomerBusy, ProfileError
from service_test_mcp.scenario.runner import Runner
from service_test_mcp.scenario.template import Context

SVC = "MobilBankacilik"


def test_call_logs_in_once_and_reuses_the_session(mobile):
    pre = mobile.profile_call(SVC, "ApplicationPreData")          # no session needed, no login
    assert pre["business"]["success"] is True and demo_mobile.STATS["app_logins"] == 0
    dash = mobile.profile_call(SVC, "GetDashBoardMobileInformation", {"MainResourceCode": "DASHBOARD"})
    assert dash["business"] == {"success": True}
    assert dash["body"]["Value"]["CustomerName"] == "Ayşe Yılmaz" and dash["test_customer"] == "bireysel_standart"
    mobile.profile_call(SVC, "GetProfileBasics")
    assert demo_mobile.STATS["customer_logins"] == 1               # one login for both calls
    view = mobile.result_view(dash)
    assert view["success"] is True and "1326" in view["body"]["Value"]["Accounts"][0]["Iban"]
    assert view["body"]["Value"]["Accounts"][0]["Iban"].startswith("*")   # masked for the model


def test_envelope_and_params(mobile):
    runtime = mobile.profiles[SVC]
    env = mobile.settings.environment("test")
    op = runtime.operation("getdashboardmobileinformation")       # case-insensitive
    req = runtime.build_request(env, op, {"MainResourceCode": "X", "Version": "9.9.9"}, {"SessionKey": "k1"},
                                {"CustomerId": 1000001})
    assert req["path"] == "/DashBoard/DashBoardService.svc/GetDashBoardMobileInformation"
    body = req["body"]
    assert body["MethodName"] == "GetDashBoardMobileInformation" and body["ExtUSessionKey"] == "k1"
    assert body["Version"] == "9.9.9"                               # params win over the envelope
    assert body["OwnerCustomerId"] == 1000001                       # session_envelope with a test_customer
    pre = runtime.build_request(env, runtime.operation("ApplicationPreData"), None, {"SessionKey": ""}, {})
    assert "OwnerCustomerId" not in pre["body"]


def test_business_failure_is_reported(mobile):
    res = mobile.profile_call(SVC, "ExecuteMoneyTransfer", {"ToIban": "TR320010009999901234567890", "Amount": "0"})
    assert res["status"] == 200 and res["business"]["success"] is False
    assert "INVALID_AMOUNT" in res["business"]["errors"]
    assert mobile.result_view(res)["success"] is False


def test_unknown_operation_and_test_customer(mobile):
    with pytest.raises(ProfileError, match="not in the catalog.*similar: GetDashBoardMobileInformation"):
        mobile.profile_call(SVC, "DashBoard")
    with pytest.raises(ProfileError, match="Unknown test_customer 'yok'"):
        mobile.profile_call(SVC, "GetProfileBasics", test_customer="yok")


def test_proactive_refresh_before_the_session_runs_out(mobile):
    mobile.profile_call(SVC, "GetProfileBasics")
    st = mobile.profiles[SVC].sessions[("test", "bireysel_standart")]
    st.created -= 280                                               # 4m40s old: inside the 30 s margin
    mobile.profile_call(SVC, "GetProfileBasics")
    assert st.logins == 2 and demo_mobile.STATS["customer_logins"] == 2


def test_server_ended_session_is_renewed_and_read_call_repeated(mobile):
    mobile.profile_call(SVC, "GetProfileBasics")
    demo_mobile.SESSIONS.clear()                                    # the server forgets the session
    res = mobile.profile_call(SVC, "GetProfileBasics")
    assert res["business"]["success"] is True and res["session_renewed"] is True
    assert demo_mobile.STATS["customer_logins"] == 2


def test_mutating_call_is_not_repeated_after_session_expiry(mobile):
    mobile.profile_call(SVC, "GetProfileBasics")
    demo_mobile.SESSIONS.clear()
    res = mobile.profile_call(SVC, "ExecuteMoneyTransfer", {"ToIban": "TR320010009999901234567890", "Amount": "5"})
    assert res["business"]["success"] is False and "not repeated" in res["session_expired"]
    assert demo_mobile.STATS["transfers"] == 0
    ok = mobile.profile_call(SVC, "ExecuteMoneyTransfer", {"ToIban": "TR320010009999901234567890", "Amount": "5"})
    assert ok["business"]["success"] is True and demo_mobile.STATS["transfers"] == 1


def test_mutating_guard(tmp_path, demo_port, monkeypatch):
    from service_test_mcp.engine import Engine

    from .conftest import write_mobile_config

    demo_mobile.reset()
    monkeypatch.setenv("STM_CONFIG", str(write_mobile_config(tmp_path, demo_port, allow_mutating=False)))
    monkeypatch.setenv("STM_HOME", str(tmp_path / "home"))
    e = Engine()
    try:
        with pytest.raises(ProfileError, match="is mutating.*allow_mutating: false"):
            e.profile_call(SVC, "ExecuteMoneyTransfer", {"Amount": "1"})
        e.profiles[SVC].profile.catalog.operations["GetProfileBasics"].kind = "unknown"
        with pytest.raises(ProfileError, match="not classified"):
            e.profile_call(SVC, "GetProfileBasics")
        assert demo_mobile.STATS["transfers"] == 0
    finally:
        e.close()


def test_parallel_login_with_the_same_customer_is_refused(mobile_config):
    from service_test_mcp.engine import Engine

    a, b = Engine(), Engine()                     # two processes / VS Code windows / agents
    try:
        a.profile_call(SVC, "GetProfileBasics")
        with pytest.raises(CustomerBusy, match="parallel logins"):
            b.profile_call(SVC, "GetProfileBasics")
        status = b.profiles[SVC].status("test")
        assert "bireysel_standart" in status["busy_elsewhere"]
        # a pool hands out a free customer
        x = a.profile_call(SVC, "GetProfileBasics", test_customer="bireysel_havuz")
        y = b.profile_call(SVC, "GetProfileBasics", test_customer="bireysel_havuz")
        assert {x["body"]["Value"]["CustomerId"], y["body"]["Value"]["CustomerId"]} == {1000002, 1000003}
        with pytest.raises(CustomerBusy):
            Engine().profile_call(SVC, "GetProfileBasics", test_customer="bireysel_havuz")
        assert demo_mobile.STATS["ended_by_parallel_login"] == 0
        # ending the session frees the customer
        ended = a.profiles[SVC].end_sessions(test_customer="bireysel_standart")
        assert ended[0]["logged_out"] is True
        assert b.profile_call(SVC, "GetProfileBasics")["business"]["success"] is True
    finally:
        a.close()
        b.close()


def test_same_customer_under_two_test_customer_names_in_one_process(mobile):
    test_customers = mobile.profiles[SVC].profile.environment("test").test_customers
    from service_test_mcp.profile.model import CustomerSpec

    first = dict(test_customers["bireysel_standart"].pool[0])
    test_customers["takma_ad"] = CustomerSpec(name="takma_ad", pool=[first])
    mobile.profile_call(SVC, "GetProfileBasics", test_customer="bireysel_standart")
    with pytest.raises(CustomerBusy, match="this process"):
        mobile.profile_call(SVC, "GetProfileBasics", test_customer="takma_ad")


def test_api_sourced_test_customer(mobile):
    res = mobile.profile_call(SVC, "GetProfileBasics", test_customer="yeni_musteri")
    assert res["business"]["success"] is True and res["body"]["Value"]["CustomerId"] == 3000001
    assert mobile.profiles[SVC].test_customer_values("test", "yeni_musteri")["CustomerId"] == 3000001


def test_lost_lease_means_new_login(mobile):
    mobile.profile_call(SVC, "GetProfileBasics")
    st = mobile.profiles[SVC].sessions[("test", "bireysel_standart")]
    mobile.leases.release(st.lease_key, mobile.owner)               # e.g. expired during a long pause
    mobile.profile_call(SVC, "GetProfileBasics")
    assert demo_mobile.STATS["customer_logins"] == 2


def test_lease_store(tmp_path):
    store = FileLeaseStore(tmp_path)
    assert store.acquire("k", "a", 60, {"test_customer": "p"}) is True
    assert store.acquire("k", "b", 60) is False
    assert store.acquire("k", "a", 60, {"test_customer": "p"}) is True    # re-entrant for the holder
    assert store.holder("k")["owner"] == "a" and store.holder("k")["test_customer"] == "p"
    store.release("k", "b")                                          # not the owner: no effect
    assert store.renew("k", "a", 0.05) is True
    time.sleep(0.1)
    assert store.renew("k", "a", 60) is False and store.holder("k") is None
    assert store.acquire("k", "b", 60) is True
    store.release("k", "b")
    assert store.holder("k") is None and not list(tmp_path.glob("*.mutex"))


def test_profile_validation(tmp_path):
    def load(text: str) -> Profile:
        p = tmp_path / "p.yaml"
        p.write_text(text, encoding="utf-8")
        return Profile.load(p)

    with pytest.raises(ConfigError, match="must contain {operation}"):
        load("request: { url: '{path}' }")
    with pytest.raises(ConfigError, match="refresh_margin_s must be smaller"):
        load("session: { ttl_s: 10, refresh_margin_s: 10 }")
    with pytest.raises(ConfigError, match="unknown setting"):
        load("sesion: {}")
    with pytest.raises(ConfigError, match="needs exactly one operator"):
        load("success: [ { path: $.Success } ]")
    with pytest.raises(ConfigError, match="'pool' test_customer has no other fields"):
        load("environments: { test: { test_customers: { a: { pool: [ {x: 1} ], y: 2 } } } }")
    p = load("catalog: c.yaml")
    assert p.catalog.operations == {} and p.catalog.path == (tmp_path / "c.yaml").resolve()


def test_template_keeps_test_customer_and_session_for_the_profile():
    ctx = Context(variables={"a": 1}, keep=("test_customer", "session"))
    assert ctx.render({"x": "{{test_customer.CustomerId}}", "y": "id={{test_customer.Id}}-{{a}}", "z": "{{a}}"}) == \
        {"x": "{{test_customer.CustomerId}}", "y": "id={{test_customer.Id}}-1", "z": 1}


def test_example_scenarios_run_on_both_environments(mobile):
    root = Path(mobile.settings.scenario_root) / "scenarios"
    for env in ("test", "prep"):
        out = Runner(mobile).run(str(root), environment=env, verbosity="failures")
        assert out["summary"]["failed"] == 0, out
        assert out["summary"]["scenarios"] == 3                     # _taslak-* drafts are not run
    steps = out["results"][1]["steps"]
    assert steps[2]["status"] == "passed" and steps[2].get("business", {}).get("success") is not True


def test_scenario_validation_accepts_profile_steps(mobile):
    runner = Runner(mobile)
    ok = runner.validate(content="id: x\ntest_customer: p\nsteps:\n  - service: MobilBankacilik\n"
                                 "    operation: Logout\n    params: { A: '{{test_customer.CustomerId}}' }\n"
                                 "    assert: [ { success: true } ]\n")
    assert ok["valid"] is True, ok
    bad = runner.validate(content="id: x\nsteps:\n  - service: MobilBankacilik\n    method: GET\n    path: /x\n")
    assert bad["valid"] is False and "profile step needs 'operation'" in " ".join(bad["problems"])


def test_hooks_file(mobile, tmp_path):
    from service_test_mcp.profile.runtime import ProfileRuntime

    profile_dir = Path(mobile.profiles[SVC].profile.path).parent
    (profile_dir / "hooks.py").write_text(
        "class Imza:\n"
        "    def __init__(self, profile):\n"
        "        self.name = profile.name\n"
        "    def prepare(self, request, ctx):\n"
        "        request['headers']['X-Op'] = ctx['operation'] + ':' + self.name\n"
        "    def inspect(self, result, ctx):\n"
        "        result['hooked'] = True\n", encoding="utf-8")
    profile = mobile.profiles[SVC].profile
    profile.hooks = "hooks.py:Imza"
    runtime = ProfileRuntime(mobile, SVC, profile, mobile.leases, mobile.owner)
    env = mobile.settings.environment("test")
    req = runtime.build_request(env, runtime.operation("ApplicationPreData"), None, {"SessionKey": ""}, {})
    assert req["headers"] == {"X-Op": "ApplicationPreData:mobile-wcf"}
    res = runtime.call("ApplicationPreData")
    assert res["hooked"] is True and res["business"]["success"] is True
    profile.hooks = "hooks.py:Yok"
    with pytest.raises(ConfigError, match="Yok not found"):
        ProfileRuntime(mobile, SVC, profile, mobile.leases, mobile.owner)
