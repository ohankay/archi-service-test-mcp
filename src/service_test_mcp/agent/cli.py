"""``service-test-agent`` - a terminal agent that tests services through service-test-mcp, without VS Code.

    service-test-agent                                   interactive chat
    service-test-agent --prompt "HesapServisi Login'i test et"
    service-test-agent --prompt-file gorev.txt --output rapor.md
    service-test-agent --mcp-url http://testsrv:8765/mcp ...  use a running MCP server (HTTP) instead of starting one
    service-test-agent --check                           test the LLM connection and list the MCP tools

LLM settings (environment variables or flags):
    STM_AGENT_BASE_URL          http://qwen.sirket.local:8000/v1  |  http://localhost:11434/v1 (Ollama)
    STM_AGENT_MODEL             qwen2.5-72b-instruct
    STM_AGENT_API_KEY           (optional) bearer key; for Azure OpenAI the api-key
    STM_AGENT_AZURE_API_VERSION (optional) e.g. 2024-10-21 -> Azure OpenAI deployment URL style
    STM_AGENT_VERIFY_TLS        true | false | path to CA bundle
    STM_AGENT_SYSTEM_PROMPT     (optional) path to a text file replacing the default system prompt
    STM_AGENT_TOOLSET           basic (default, 8 tools) | expert (every tool)
The MCP server it starts reads the usual STM_CONFIG / service secrets from the same environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio

from ..server import TOOLSETS, toolset_name
from .core import DEFAULT_SYSTEM_PROMPT, Agent, AgentConfig, mcp_session
from .llm import LLMConfig, LLMError


def _verify(value: str | None) -> bool | str:
    if value is None or value.strip() == "":
        return True
    if value.strip().lower() in ("0", "false", "no", "off"):
        return False
    if value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return value


def _printer(verbose: bool):
    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "tool_call":
            args = json.dumps(data["arguments"], ensure_ascii=False)
            print(f"  -> {data['name']}({args[:300]}{'...' if len(args) > 300 else ''})", file=sys.stderr, flush=True)
        elif kind == "tool_result" and verbose:
            out = data["output"]
            print(f"  <- {out[:600]}{'...' if len(out) > 600 else ''}", file=sys.stderr, flush=True)
        elif kind == "tools" and verbose:
            print(f"[mcp] {len(data['names'])} tools: {', '.join(data['names'])}", file=sys.stderr, flush=True)
    return on_event


def build_config(args: argparse.Namespace) -> AgentConfig:
    base_url = args.base_url or os.environ.get("STM_AGENT_BASE_URL")
    model = args.model or os.environ.get("STM_AGENT_MODEL")
    if not base_url or not model:
        raise SystemExit("LLM is not configured: set STM_AGENT_BASE_URL and STM_AGENT_MODEL (or --base-url/--model).")
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    prompt_file = args.system_prompt or os.environ.get("STM_AGENT_SYSTEM_PROMPT")
    system_prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else DEFAULT_SYSTEM_PROMPT
    llm = LLMConfig(base_url=base_url, model=model, api_key=api_key,
                    azure_api_version=os.environ.get("STM_AGENT_AZURE_API_VERSION") or None,
                    temperature=args.temperature, verify_tls=_verify(os.environ.get("STM_AGENT_VERIFY_TLS")))
    toolset = toolset_name(args.toolset or os.environ.get("STM_AGENT_TOOLSET") or os.environ.get("STM_TOOLSET"))
    cfg = AgentConfig(llm=llm, mcp_url=args.mcp_url or os.environ.get("STM_AGENT_MCP_URL") or None,
                      mcp_token=os.environ.get("STM_HTTP_TOKEN") or None, system_prompt=system_prompt,
                      max_steps=args.max_steps, allowed_tools=list(TOOLSETS[toolset]))
    cfg.server_env = {**os.environ, "STM_TOOLSET": toolset}   # the server it starts offers the same set
    return cfg


async def _check(cfg: AgentConfig) -> int:
    from .llm import ChatClient

    ok = True
    client = ChatClient(cfg.llm)
    try:
        msg = await anyio.to_thread.run_sync(client.complete, [{"role": "user", "content": "Reply with: OK"}], None)
        print(f"LLM ok ({cfg.llm.model} @ {cfg.llm.base_url}): {str(msg.get('content'))[:80]!r}")
    except LLMError as exc:
        ok = False
        print(f"LLM FAILED: {exc}")
    finally:
        client.close()
    try:
        async with mcp_session(cfg) as session:
            tools = (await session.list_tools()).tools
            print(f"MCP ok ({cfg.mcp_url or 'stdio'}): {len(tools)} tools")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"MCP FAILED: {type(exc).__name__}: {exc}")
    return 0 if ok else 1


async def _main(args: argparse.Namespace) -> int:
    cfg = build_config(args)
    if args.check:
        return await _check(cfg)
    prompts: list[str] = []
    if args.prompt:
        prompts.append(args.prompt)
    if args.prompt_file:
        prompts.append(Path(args.prompt_file).read_text(encoding="utf-8"))
    agent = Agent(cfg, _printer(args.verbose))
    answers: list[str] = []
    try:
        async with mcp_session(cfg) as session:
            await agent.start(session)
            if prompts:
                for p in prompts:
                    answer = await agent.ask(p)
                    answers.append(answer)
                    print(answer, flush=True)
            else:
                print("service-test-agent - type your request, 'exit' to quit.", flush=True)
                while True:
                    try:
                        text = await anyio.to_thread.run_sync(input, "\nsen> ")
                    except (EOFError, KeyboardInterrupt):
                        break
                    if text.strip().lower() in ("exit", "quit"):
                        break
                    if not text.strip():
                        continue
                    try:
                        answer = await agent.ask(text)
                    except LLMError as exc:
                        print(f"[LLM error] {exc}", flush=True)
                        continue
                    answers.append(answer)
                    print(f"\nagent> {answer}", flush=True)
    except LLMError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 2
    finally:
        agent.close()
    if args.output:
        lines = []
        for item in agent.transcript:
            lines += [f"## Question\n\n{item['question']}\n", f"## Answer\n\n{item['answer']}\n"]
        Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    if args.transcript:
        Path(args.transcript).write_text(json.dumps(agent.messages, ensure_ascii=False, indent=2, default=str),
                                         encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service-test-agent",
                                     description="Standalone service-testing agent (OpenAI-compatible LLM + MCP)")
    parser.add_argument("--prompt", help="single request; without it an interactive chat starts")
    parser.add_argument("--prompt-file", help="read the request from a file")
    parser.add_argument("--output", help="write questions and answers to a Markdown file")
    parser.add_argument("--transcript", help="write the full message log (JSON), incl. tool calls")
    parser.add_argument("--base-url", help="LLM base URL (default STM_AGENT_BASE_URL)")
    parser.add_argument("--model", help="model name (default STM_AGENT_MODEL)")
    parser.add_argument("--api-key-env", default="STM_AGENT_API_KEY", help="env var holding the API key")
    parser.add_argument("--system-prompt", help="file replacing the default system prompt")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-steps", type=int, default=25, help="max LLM turns per request")
    parser.add_argument("--toolset", choices=("basic", "expert"),
                        help="basic (default): the 8 definition/test tools; expert: also raw SOAP/REST, profile "
                             "and YAML-scenario tools (STM_AGENT_TOOLSET)")
    parser.add_argument("--mcp-url", help="streamable HTTP URL of a running server (default: start one over stdio)")
    parser.add_argument("--check", action="store_true", help="test LLM + MCP connectivity and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print tool results")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
        except Exception:  # noqa: BLE001
            pass
    return anyio.run(_main, args)


if __name__ == "__main__":
    sys.exit(main())
