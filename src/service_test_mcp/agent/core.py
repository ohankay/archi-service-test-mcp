"""Agent loop: LLM (OpenAI-compatible) <-> MCP tools of service-test-mcp.

The agent talks to the MCP server exactly like VS Code does - over stdio (it starts ``service-test-mcp`` itself) or
over streamable HTTP (``--mcp-url http://host:8765/mcp``) - so every guard (allowlist, masking, mutating block,
audit log) applies to it as well.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import anyio

from .llm import ChatClient, LLMConfig, mcp_tools_to_openai

def _default_prompt() -> str:
    """The agent instructions are kept in one file, shared with the VS Code custom agent (sync_agent_files.py)."""
    from importlib.resources import files

    return files("service_test_mcp").joinpath("prompts", "service-tester.md").read_text(encoding="utf-8")


DEFAULT_SYSTEM_PROMPT = _default_prompt()


@dataclass
class AgentConfig:
    llm: LLMConfig
    mcp_url: str | None = None
    mcp_token: str | None = None
    server_command: list[str] = field(default_factory=lambda: [sys.executable, "-m", "service_test_mcp"])
    server_env: dict[str, str] | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_steps: int = 25
    max_tool_result_chars: int = 24000
    allowed_tools: list[str] | None = None


@contextlib.asynccontextmanager
async def mcp_session(cfg: AgentConfig) -> AsyncIterator[Any]:
    from mcp import ClientSession

    if cfg.mcp_url:
        import httpx
        from mcp.client import streamable_http

        headers = {"Authorization": f"Bearer {cfg.mcp_token}"} if cfg.mcp_token else None
        if hasattr(streamable_http, "streamable_http_client"):  # mcp >= 1.24
            http = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=300), follow_redirects=True)
            transport = streamable_http.streamable_http_client(cfg.mcp_url, http_client=http)
        else:  # pragma: no cover - older mcp
            http = None
            transport = streamable_http.streamablehttp_client(cfg.mcp_url, headers=headers)
        try:
            async with transport as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        finally:
            if http is not None:
                await http.aclose()
    else:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ if cfg.server_env is None else cfg.server_env)
        params = StdioServerParameters(command=cfg.server_command[0], args=cfg.server_command[1:], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _tool_text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))
    return "\n".join(parts) if parts else "{}"


class Agent:
    def __init__(self, cfg: AgentConfig, on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.cfg = cfg
        self.client = ChatClient(cfg.llm)
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": cfg.system_prompt}]
        self.on_event = on_event or (lambda kind, data: None)
        self.session: Any = None
        self.tools: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []

    async def start(self, session: Any) -> None:
        self.session = session
        tools = (await session.list_tools()).tools
        if self.cfg.allowed_tools:
            tools = [t for t in tools if t.name in self.cfg.allowed_tools]
        self.tools = mcp_tools_to_openai(tools)
        self.on_event("tools", {"names": [t["function"]["name"] for t in self.tools]})

    async def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        for step in range(self.cfg.max_steps):
            message = await anyio.to_thread.run_sync(self.client.complete, self.messages, self.tools)
            calls = message.get("tool_calls") or []
            entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or (None if calls else "")}
            if calls:
                entry["tool_calls"] = calls
            self.messages.append(entry)
            if not calls:
                answer = message.get("content") or ""
                self.transcript.append({"question": text, "answer": answer, "steps": step + 1})
                return answer
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as exc:
                    output = json.dumps({"success": False, "error": {"type": "BadArguments", "message": str(exc)}})
                else:
                    self.on_event("tool_call", {"name": name, "arguments": args})
                    if self.tools and name not in {t["function"]["name"] for t in self.tools}:
                        output = json.dumps({"success": False, "error": {"message": f"unknown tool {name}"}})
                    else:
                        result = await self.session.call_tool(name, args)
                        output = _tool_text(result)
                if len(output) > self.cfg.max_tool_result_chars:
                    output = output[: self.cfg.max_tool_result_chars] + "...(truncated)"
                self.on_event("tool_result", {"name": name, "output": output})
                self.messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": output})
        answer = f"(stopped after {self.cfg.max_steps} steps without a final answer)"
        self.transcript.append({"question": text, "answer": answer, "steps": self.cfg.max_steps})
        return answer

    def close(self) -> None:
        self.client.close()


async def run_prompts(cfg: AgentConfig, prompts: list[str],
                      on_event: Callable[[str, dict[str, Any]], None] | None = None) -> list[str]:
    agent = Agent(cfg, on_event)
    try:
        async with mcp_session(cfg) as session:
            await agent.start(session)
            return [await agent.ask(p) for p in prompts]
    finally:
        agent.close()
