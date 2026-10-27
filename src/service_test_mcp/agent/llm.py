"""Minimal OpenAI-compatible chat-completions client (httpx, already a dependency of mcp - nothing extra to install).

Works with any server that speaks ``POST {base_url}/chat/completions`` with function/tool calling:
vLLM / SGLang / TGI (e.g. a company Qwen server), Ollama (``http://localhost:11434/v1``), LM Studio, OpenAI,
Azure OpenAI (set ``azure_api_version``), LiteLLM proxies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None = None
    azure_api_version: str | None = None
    temperature: float = 0.0
    timeout_s: float = 180.0
    verify_tls: bool | str = True
    extra_headers: dict[str, str] = field(default_factory=dict)
    max_tokens: int | None = None


class ChatClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        headers = {"Content-Type": "application/json", **config.extra_headers}
        if config.api_key:
            if config.azure_api_version:
                headers["api-key"] = config.api_key
            else:
                headers["Authorization"] = f"Bearer {config.api_key}"
        self._http = httpx.Client(timeout=config.timeout_s, verify=config.verify_tls, headers=headers)

    def close(self) -> None:
        self._http.close()

    @property
    def url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if self.config.azure_api_version:
            return f"{base}/openai/deployments/{self.config.model}/chat/completions?api-version=" \
                   f"{self.config.azure_api_version}"
        return f"{base}/chat/completions"

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages,
                                   "temperature": self.config.temperature}
        if self.config.max_tokens:
            payload["max_tokens"] = self.config.max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            resp = self._http.post(self.url, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM server not reachable at {self.url}: {exc}") from None
        if resp.status_code >= 400:
            text = resp.text[:1500]
            hint = ""
            if tools and resp.status_code == 400 and "tool" in text.lower():
                hint = (" - the model server may not have tool calling enabled (vLLM: --enable-auto-tool-choice "
                        "--tool-call-parser hermes for Qwen)")
            raise LLMError(f"LLM HTTP {resp.status_code}: {text}{hint}")
        data = resp.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unexpected LLM response: {str(data)[:1000]}") from None
        return message


def mcp_tools_to_openai(tools: list[Any]) -> list[dict[str, Any]]:
    out = []
    for tool in tools:
        schema = dict(tool.inputSchema or {"type": "object", "properties": {}})
        schema.pop("title", None)
        out.append({"type": "function", "function": {"name": tool.name, "description": (tool.description or "")[:1024],
                                                     "parameters": schema}})
    return out
