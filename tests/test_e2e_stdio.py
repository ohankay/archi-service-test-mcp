"""End to end: spawn the server and talk MCP exactly like VS Code (stdio) or an external agent (HTTP)."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from service_test_mcp.server import PROFILE_TOOL_NAMES, TOOL_NAMES

from .conftest import clean_env, write_config, write_mobile_config


async def _session(env, fn):
    params = StdioServerParameters(command=sys.executable, args=["-m", "service_test_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def _call(session, name, **args):
    res = await session.call_tool(name, args)
    return json.loads(res.content[0].text)


def test_stdio_without_configuration(tmp_path):
    async def scenario(s):
        tools = (await s.list_tools()).tools
        assert {t.name for t in tools} == set(TOOL_NAMES)
        by = {t.name: t for t in tools}
        assert by["list_services"].annotations.readOnlyHint is True
        assert by["soap_call"].annotations.openWorldHint is True
        res = await _call(s, "list_services")
        assert res["success"] is True and "STM_CONFIG" in res["note"]
        res = await _call(s, "soap_call", service="X", operation="Y")
        assert res["success"] is False and "No services configured" in res["error"]["message"]
    asyncio.run(_session(clean_env(STM_HOME=str(tmp_path)), scenario))


def test_stdio_end_to_end(tmp_path, demo_port):
    cfg = write_config(tmp_path, demo_port)

    async def scenario(s):
        res = await _call(s, "list_services")
        assert {x["name"] for x in res["services"]} == {"HesapServisi", "HesapApi"}
        res = await _call(s, "check_service")
        assert res["all_ok"] is True, res
        res = await _call(s, "describe_operation", service="HesapServisi", operation="Login")
        assert res["example_params"] == {"kullaniciAdi": "string", "sifre": "string"}
        res = await _call(s, "soap_call", service="HesapServisi", operation="Login",
                          params={"kullaniciAdi": "{{env.kullanici}}", "sifre": "{{env.demo_sifre}}"},
                          extract={"token": "$.Token", "musteriNo": "$.MusteriNo"})
        assert res["success"] and res["body"]["Token"] == "***" and res["saved_variables"]["token"] == "***"
        res = await _call(s, "soap_call", service="HesapServisi", operation="GetBakiye",
                          params={"token": "{{token}}", "musteriNo": "{{musteriNo}}"})
        assert res["success"] is True and res["body"]["Basarili"] is True
        res = await _call(s, "check_last_response", assertions=[
            {"path": "$.Hesaplar.Hesap", "count": 2}, {"path": "$.ToplamBakiyeTRY", "numeric": {"gt": 10000}}])
        assert res["passed"] is False and res["assertions"][0]["passed"] is True
        res = await _call(s, "rest_call", service="HesapApi", method="GET", path="/hesaplar/1000001-1",
                          headers={"Authorization": "Bearer {{token}}"})
        assert res["status"] == 200 and res["body"]["durum"] == "AKTIF"
        res = await _call(s, "soap_call", service="HesapServisi", operation="TransferYap", params={})
        assert res["success"] is False and "mutating" in res["error"]["message"]
        res = await _call(s, "validate_scenario", path=str(tmp_path / "scenarios"))
        assert res["valid"] is True and len(res["scenarios"]) == 3
        res = await _call(s, "run_test_suite", path=str(tmp_path / "scenarios"), junit_path=str(tmp_path / "j.xml"))
        assert res["summary"]["passed"] == 3, res
        res = await _call(s, "run_scenario", content="id: x\nsteps:\n  - service: HesapApi\n    method: GET\n"
                                                    "    path: /\n    assert:\n      - path: $.status\n"
                                                    "        equals: down\n")
        assert res["results"][0]["passed"] is False
        assert res["results"][0]["steps"][0]["assertions"][0]["actual"] == "ok"
    asyncio.run(_session(clean_env(STM_CONFIG=str(cfg), STM_HOME=str(tmp_path / "home")), scenario))


def test_stdio_mobile_profile(tmp_path, demo_port):
    """VS Code's view of the mobile profile: catalog, call by operation name, test_customers, log import."""
    from service_test_mcp import demo_mobile

    demo_mobile.reset()
    cfg = write_mobile_config(tmp_path, demo_port)
    log = cfg.parent / "logs" / "ornek-prep-transaction.log"

    async def scenario(s):
        res = await _call(s, "list_services")
        svc = res["services"][0]
        assert svc["call_with"] == "call_operation" and svc["login_steps"][-1] == "OtpLoginCompleteNew"
        assert "hedef_iban" in res["environments"]["test"]["variables"]
        res = await _call(s, "list_operations", service="MobilBankacilik", kind="read")
        assert "GetDashBoardMobileInformation" in {o["operation"] for o in res["operations"]}
        res = await _call(s, "describe_operation", service="MobilBankacilik", operation="ExecuteMoneyTransfer")
        assert res["kind"] == "mutating" and res["allowed_here"] is True and "Amount" in res["params"]
        res = await _call(s, "call_operation", service="MobilBankacilik", operation="GetDashBoardMobileInformation",
                          params={"MainResourceCode": "DASHBOARD"}, extract={"bakiye": "$.Value.Accounts[0].Balance"},
                          assertions=[{"success": True}, {"path": "$.Value.Accounts", "count": 1}])
        assert res["success"] is True and res["assertions_passed"] is True
        assert res["test_customer"] == "bireysel_standart"
        assert res["body"]["Value"]["Accounts"][0]["Iban"].endswith("1326") and "TR33" not in json.dumps(res)
        res = await _call(s, "call_operation", service="MobilBankacilik", operation="ExecuteMoneyTransfer",
                          params={"ToIban": "{{env.hedef_iban}}", "Amount": "1.00", "Description": "e2e",
                                  "TransferType": 1})
        assert res["success"] is True and res["business"] == {"success": True}
        res = await _call(s, "list_test_customers", service="MobilBankacilik", environment="test")
        assert res["sessions"][0]["test_customer"] == "bireysel_standart" and res["sessions"][0]["logins"] == 1
        res = await _call(s, "import_traffic_log", path=str(log))
        assert res["applied"] is False and res["services"][0]["summary"] == {"unchanged": 8}
        assert demo_mobile.APP_PASSWORD not in json.dumps(res)
        res = await _call(s, "run_test_suite", path=str(cfg.parent / "scenarios"), environment="prep")
        assert res["summary"]["passed"] == 3, res
        res = await _call(s, "end_sessions")
        assert {x["test_customer"] for x in res["ended"]} >= {"bireysel_standart"}
        assert all(x["logged_out"] for x in res["ended"])
    asyncio.run(_session(clean_env(STM_CONFIG=str(cfg), STM_HOME=str(tmp_path / "home")), scenario))
    assert demo_mobile.STATS["ended_by_parallel_login"] == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_transport_with_token(tmp_path, demo_port):
    from service_test_mcp.agent.core import AgentConfig, mcp_session
    from service_test_mcp.agent.llm import LLMConfig

    cfg = write_config(tmp_path, demo_port)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "service_test_mcp", "--transport", "http", "--port", str(port)],
                            env=clean_env(STM_CONFIG=str(cfg), STM_HOME=str(tmp_path / "home"),
                                          STM_HTTP_TOKEN="t0ken-123"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        import httpx

        assert httpx.post(f"http://127.0.0.1:{port}/mcp", json={}).status_code == 401

        async def go():
            agent_cfg = AgentConfig(llm=LLMConfig(base_url="http://unused", model="x"),
                                    mcp_url=f"http://127.0.0.1:{port}/mcp", mcp_token="t0ken-123")
            async with mcp_session(agent_cfg) as s:
                res = await _call(s, "list_services")
                assert len(res["services"]) == 2
        asyncio.run(go())
    finally:
        proc.terminate()
        proc.wait(10)


# -- standalone agent with a fake OpenAI-compatible LLM ------------------------------------------------------------
class _FakeLLM(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeLLM.calls.append(body)
        last = body["messages"][-1]
        if last["role"] == "user":
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "soap_call", "arguments": json.dumps(
                    {"service": "HesapServisi", "operation": "Login",
                     "params": {"kullaniciAdi": "demo", "sifre": "{{env.demo_sifre}}"}})}}]}
        else:
            result = json.loads(last["content"])
            msg = {"role": "assistant", "content": f"Login sonucu: Basarili={result['body']['Basarili']}"}
        data = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_standalone_agent(tmp_path, demo_port):
    from service_test_mcp.agent.core import AgentConfig, run_prompts
    from service_test_mcp.agent.llm import LLMConfig

    llm = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    cfg_file = write_config(tmp_path, demo_port)
    cfg = AgentConfig(llm=LLMConfig(base_url=f"http://127.0.0.1:{llm.server_port}/v1", model="fake", api_key="k"),
                      server_env=clean_env(STM_CONFIG=str(cfg_file), STM_HOME=str(tmp_path / "home")))
    events = []
    answers = asyncio.run(run_prompts(cfg, ["Login'i test et"], lambda k, d: events.append(k)))
    llm.shutdown()
    assert answers == ["Login sonucu: Basarili=True"]
    assert "tool_call" in events and "tool_result" in events
    first = _FakeLLM.calls[0]
    assert {t["function"]["name"] for t in first["tools"]} == set(TOOL_NAMES)
    tool_msg = _FakeLLM.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "Demo123!" not in tool_msg["content"]


class _FakeProfileLLM(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeProfileLLM.calls.append(body)
        last = body["messages"][-1]
        if last["role"] == "user":
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "call_operation", "arguments": json.dumps(
                    {"service": "MobilBankacilik", "operation": "GetProfileBasics"})}}]}
        else:
            result = json.loads(last["content"])
            msg = {"role": "assistant", "content": f"basari={result['success']}"}
        data = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_standalone_agent_profile_toolset(tmp_path, demo_port):
    from service_test_mcp import demo_mobile
    from service_test_mcp.agent.cli import build_config, main
    from service_test_mcp.agent.core import run_prompts

    demo_mobile.reset()
    llm = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProfileLLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    cfg_file = write_mobile_config(tmp_path, demo_port)
    import argparse

    args = argparse.Namespace(base_url=f"http://127.0.0.1:{llm.server_port}/v1", model="fake", api_key_env=None,
                              system_prompt=None, temperature=0.0, mcp_url=None, max_steps=5, toolset="profile")
    cfg = build_config(args)
    cfg.server_env = clean_env(STM_CONFIG=str(cfg_file), STM_HOME=str(tmp_path / "home"))
    answers = asyncio.run(run_prompts(cfg, ["profil bilgilerini kontrol et"]))
    llm.shutdown()
    assert answers == ["basari=True"]
    first = _FakeProfileLLM.calls[0]
    assert {t["function"]["name"] for t in first["tools"]} == set(PROFILE_TOOL_NAMES)
    assert "call_operation" in first["messages"][0]["content"]       # shared agent instructions
    assert main is not None
