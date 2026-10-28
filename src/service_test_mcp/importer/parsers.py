"""Traffic log parsers: turn a log file into request/response pairs (``Exchange``).

The MCP server reads the file; the model never sees the raw log. Supported formats (detected automatically):

* ``transaction_log`` - the mobile app's log: blocks separated by a line of ``*`` with ``Date:``, ``Method:``,
  ``Method Name:``, ``URL:`` and ``RequestData:`` / ``ResponseData:`` followed by JSON. Responses are paired with
  the oldest unanswered request of the same method name + URL; extra or missing responses are tolerated.
* ``har`` - HTTP Archive (browser dev tools, Fiddler, Charles, proxies). Carries headers and status codes.

A new format is one function added to ``PARSERS`` (and a line in ``detect``).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..normalize import parse_json_text

MAX_LOG_BYTES = 100 * 1024 * 1024


@dataclass
class Exchange:
    index: int
    method: str
    url: str
    name: str | None = None
    timestamp: str | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    has_response: bool = False
    extra_responses: int = 0


def _body(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return parse_json_text(text)
    except ValueError:
        return text


_SEPARATOR = re.compile(r"^\*{10,}\s*$", re.M)
_DATA = re.compile(r"^(RequestData|ResponseData)\s*:", re.M)
_HEADER = re.compile(r"^([A-Za-z][A-Za-z ]*?)\s*:\s*(.*)$")


def _http_method(value: str | None) -> str:
    """'post', 'POST' or the iOS app's 'HTTPMethod(rawValue: "POST")' -> 'POST'."""
    m = re.search(r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)", str(value or ""), re.IGNORECASE)
    return m.group(1).upper() if m else "POST"


def parse_transaction_log(text: str) -> tuple[list[Exchange], list[str]]:
    exchanges: list[Exchange] = []
    warnings: list[str] = []
    pending: dict[tuple[str, str], deque[Exchange]] = defaultdict(deque)
    last: dict[tuple[str, str], Exchange] = {}
    orphans = 0
    for block in _SEPARATOR.split(text):
        if not block.strip():
            continue
        match = _DATA.search(block)
        if not match:
            continue
        meta: dict[str, str] = {}
        for line in block[: match.start()].splitlines():
            m = _HEADER.match(line.strip())
            if m:
                meta[m.group(1).strip().lower()] = m.group(2).strip()
        url = meta.get("url", "")
        name = meta.get("method name") or url.rstrip("/").rsplit("/", 1)[-1]
        key = (name, url)
        body = _body(block[match.end():])
        if match.group(1) == "RequestData":
            ex = Exchange(index=len(exchanges), method=_http_method(meta.get("method")), url=url, name=name,
                          timestamp=meta.get("date"), request_body=body)
            exchanges.append(ex)
            pending[key].append(ex)
            last[key] = ex
        else:
            if pending[key]:
                ex = pending[key].popleft()
                ex.response_body, ex.has_response = body, True
                ex.status = 200
            elif key in last:
                last[key].extra_responses += 1  # the app logged the same response more than once
            else:
                orphans += 1
    unanswered = sum(len(q) for q in pending.values())
    if unanswered:
        warnings.append(f"{unanswered} request(s) have no logged response (kept, without response schema)")
    if orphans:
        warnings.append(f"{orphans} response(s) without a matching request were skipped")
    return exchanges, warnings


def parse_har(text: str) -> tuple[list[Exchange], list[str]]:
    data = json.loads(text)
    entries = ((data or {}).get("log") or {}).get("entries") or []
    exchanges: list[Exchange] = []
    for entry in entries:
        req = entry.get("request") or {}
        res = entry.get("response") or {}
        post = (req.get("postData") or {}).get("text")
        content = (res.get("content") or {}).get("text")
        if (res.get("content") or {}).get("encoding") == "base64":
            content = None
        url = str(req.get("url") or "")
        exchanges.append(Exchange(
            index=len(exchanges), method=str(req.get("method") or "GET").upper(), url=url,
            name=url.split("?")[0].rstrip("/").rsplit("/", 1)[-1], timestamp=entry.get("startedDateTime"),
            request_headers={h.get("name", ""): h.get("value", "") for h in req.get("headers") or []},
            request_body=_body(post or ""), status=res.get("status") or None,
            response_headers={h.get("name", ""): h.get("value", "") for h in res.get("headers") or []},
            response_body=_body(content or ""), has_response=bool(res)))
    return exchanges, []


PARSERS: dict[str, Callable[[str], tuple[list[Exchange], list[str]]]] = {
    "transaction_log": parse_transaction_log,
    "har": parse_har,
}


def detect(text: str) -> str | None:
    head = text.lstrip()[:4000]
    if head.startswith("{") and '"log"' in head and '"entries"' in text[:200000]:
        return "har"
    if _SEPARATOR.search(head) and ("Method Name" in head or "RequestData" in text[:200000]):
        return "transaction_log"
    return None


def parse_traffic(text: str, fmt: str | None = None) -> tuple[str, list[Exchange], list[str]]:
    fmt = fmt or detect(text)
    if fmt is None:
        raise ValueError(f"Unrecognized log format. Supported: {', '.join(PARSERS)} (give 'format' if detection fails)")
    if fmt not in PARSERS:
        raise ValueError(f"Unknown format '{fmt}'. Supported: {', '.join(PARSERS)}")
    exchanges, warnings = PARSERS[fmt](text)
    return fmt, exchanges, warnings
