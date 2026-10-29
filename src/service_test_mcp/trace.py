"""Trace: every HTTP exchange made while a tool runs, so nothing the server does is hidden.

For each exchange the trace keeps what was really sent and received: method, full URL, headers (including the ones
the session adds), body, timeout, proxy, TLS verification, status, response headers and body, redirects, time, the
business result and - when it failed - what kind of failure it was (DNS, TCP connect, TLS, proxy, no response in
time ...) with the whole cause chain.

The tool result gets:

* ``problem``     one sentence about the first exchange that went wrong (the step and why)
* ``flow``        one readable line per exchange ("2) [login 2/4] POST host/.../InitialLogin -> HTTP 200 ...")
* ``trace``       the exchanges that went wrong, short (bodies shaped to STM_TRACE_BODY_CHARS; STM_TRACE=all: all)
* ``trace_file``  the complete trace as JSON in %USERPROFILE%\\.service-test-mcp\\traces\\ (open it in VS Code)
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .config import env_setting, home_dir

_active: ContextVar[list[dict[str, Any]] | None] = ContextVar("stm_trace", default=None)
FILES_KEPT = 200


@contextlib.contextmanager
def collect() -> Iterator[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    token = _active.set(records)
    try:
        yield records
    finally:
        _active.reset(token)


def add(record: dict[str, Any]) -> dict[str, Any]:
    records = _active.get()
    if records is not None:
        record = {"no": len(records) + 1, **record}
        records.append(record)
    return record


def last() -> dict[str, Any] | None:
    records = _active.get()
    return records[-1] if records else None


# -- errors ---------------------------------------------------------------------------------------------------------
def cause_chain(exc: BaseException) -> list[str]:
    out, seen = [], set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        out.append(f"{type(current).__name__}: {str(current)[:500]}")
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException) and id(reason) not in seen:
            current = reason
            continue
        current = current.__cause__ or current.__context__
    return out


def classify(exc: BaseException, url: str, timeout: Any, proxy: str | None) -> dict[str, Any]:
    """A request exception -> what happened, in which phase, and what usually causes it."""
    import requests

    chain = cause_chain(exc)
    text = " | ".join(chain)
    host = re.sub(r"^\w+://", "", url).split("/")[0]
    out: dict[str, Any] = {"kind": type(exc).__name__, "chain": chain}
    if isinstance(exc, requests.exceptions.ConnectTimeout) or "ConnectTimeout" in text:
        out["phase"] = (f"no connection: a TCP/TLS connection to {host} did not open in {timeout} s "
                        f"(proxy: {proxy or 'none'})")
        out["likely_causes"] = ["No network path from this computer to the server (VPN off / outside the company "
                                "network)", "A company proxy is needed but not used ('proxy' of the environment in "
                                "the system definition, or HTTPS_PROXY)", "A firewall drops the connection"]
    elif "handshake operation timed out" in text or "_ssl.c" in text and "timed out" in text:
        # urllib3 reports this as a read timeout, but no request was sent: TLS never completed
        out["phase"] = (f"TCP connection opened but the TLS handshake did not complete in {timeout} s; the request "
                        f"was never sent (proxy: {proxy or 'none, direct connection'})")
        out["likely_causes"] = (["The proxy does not complete this connection: proxy address / authentication"]
                                if proxy else
                                ["The company network does not allow a direct connection to this address; it has to "
                                 "go through a proxy. The browser may use the Windows proxy / PAC (automatic "
                                 "configuration); Python does not read the PAC file. check_connection shows the "
                                 "Windows proxy setting; write the proxy it finds as 'proxy:' of the environment",
                                 "A firewall drops TLS traffic to this server (VPN off / outside the company network)",
                                 "The server (or its load balancer) does not complete the handshake with this "
                                 "client"])
    elif isinstance(exc, requests.exceptions.ReadTimeout) or "ReadTimeout" in text or "Read timed out" in text:
        out["phase"] = f"connected and the request was sent; the server did not answer in {timeout} s"
        out["likely_causes"] = ["The server is slow or the request is stuck (timeout_s can be raised)",
                                "The request body is not what the server expects and it does not answer"]
    elif isinstance(exc, requests.exceptions.ProxyError) or "ProxyError" in text:
        out["phase"] = f"proxy error ({proxy})"
        out["likely_causes"] = ["Wrong or unreachable proxy address", "The proxy asks for authentication"]
    elif isinstance(exc, requests.exceptions.SSLError) or "SSLError" in text or "CERTIFICATE" in text.upper():
        out["phase"] = "TLS (certificate) verification failed"
        out["likely_causes"] = ["The company's own certificate authority is not known: verify_tls: <CA .pem path> "
                                "in the environment", "A proxy in between replaces the certificate"]
    elif "NameResolution" in text or "getaddrinfo" in text or "Name or service not known" in text:
        out["phase"] = f"DNS: the name '{host}' could not be resolved"
        out["likely_causes"] = ["This computer's DNS does not know the name (VPN / company DNS needed)",
                                "The address is misspelled"]
    elif "Connection refused" in text or "ConnectionRefused" in text or "WinError 10061" in text:
        out["phase"] = f"connection refused: nothing listens on that port of {host}"
        out["likely_causes"] = ["Wrong address or port", "The service is down"]
    elif "RemoteDisconnected" in text or "Connection aborted" in text or "reset" in text.lower():
        out["phase"] = "the connection was closed by the other side"
        out["likely_causes"] = ["The server or a device in between refused the request"]
    else:
        out["phase"] = "the request could not be sent"
    return out


# -- views ------------------------------------------------------------------------------------------------------------
def _size(value: Any) -> int:
    return len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False, default=str))


def shorten(value: Any, limit: int) -> Any:
    """Shorten but keep the shape: objects stay objects (no escaped JSON strings), long lists keep their first
    items, long strings their start. The full value is in the trace file."""
    if _size(value) <= limit:
        return value
    if isinstance(value, str):
        return value[:limit] + f"... (+{len(value) - limit} characters; all in the trace file)"
    for items, text in ((5, 200), (2, 120), (1, 60), (1, 30)):
        short = _shape(value, items, text, 0)
        if _size(short) <= limit:
            return short
    return short


def _shape(value: Any, items: int, text: int, depth: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= text else value[:text] + "..."
    if isinstance(value, list):
        out = [_shape(v, items, text, depth + 1) for v in value[:items]]
        if len(value) > items:
            out.append(f"... (+{len(value) - items} items; all in the trace file)")
        return out
    if isinstance(value, dict):
        if depth >= 6:
            return f"{{... {len(value)} fields}}"
        keys = list(value)
        keep = keys if depth or len(keys) <= 40 else keys[:40]
        out = {k: _shape(value[k], items, text, depth + 1) for k in keep}
        if len(keep) < len(keys):
            out["..."] = f"+{len(keys) - len(keep)} fields; all in the trace file"
        return out
    return value


def short_url(url: str) -> str:
    """https://host/a/b/c.svc/Method?x -> host/.../c.svc/Method (the full URL is in 'trace' and the trace file)."""
    m = re.match(r"^\w+://([^/]+)(/[^?]*)?", url)
    if not m:
        return url
    parts = [p for p in (m.group(2) or "").split("/") if p]
    return m.group(1) + ("/.../" if len(parts) > 2 else "/") + "/".join(parts[-2:])


def line(r: dict[str, Any]) -> str:
    request = r.get("request") or {}
    head = f"{r.get('no')}) [{r.get('origin')}] {request.get('method', '')} {short_url(request.get('url') or '')}"
    if r.get("error"):
        e = r["error"]
        return f"{head} -> ERROR: {e.get('phase')} [{e.get('kind')}], {r.get('duration_ms')} ms"
    text = f"{head} -> HTTP {(r.get('response') or {}).get('status')}, {r.get('duration_ms')} ms"
    business = r.get("business")
    if business is not None:
        text += ", business result: " + ("success" if business.get("success") else
                                         f"failure ({'; '.join(map(str, business.get('errors') or [])) or '-'})")
    if r.get("extracted"):
        text += f", extracted: {', '.join(r['extracted'])}"
    return text


def went_wrong(r: dict[str, Any]) -> bool:
    """An exchange worth showing in full: an error, an HTTP error status or a failed business result."""
    if r.get("error"):
        return True
    status = (r.get("response") or {}).get("status")
    if isinstance(status, int) and status >= 400:
        return True
    return (r.get("business") or {}).get("success") is False


def problem(records: list[dict[str, Any]]) -> str | None:
    """One sentence about the first exchange that went wrong."""
    for r in records:
        if not went_wrong(r):
            continue
        where = f"{r.get('origin')} ({r.get('operation') or short_url((r.get('request') or {}).get('url') or '')})"
        if r.get("error"):
            return f"{where}: {r['error'].get('phase')} [{r['error'].get('kind')}]"
        status = (r.get("response") or {}).get("status")
        business = r.get("business") or {}
        return f"{where}: HTTP {status}" + (
            f", business result failure: {'; '.join(map(str, business.get('errors') or []))[:400]}"
            if business.get("success") is False else "")
    return None


def brief(records: list[dict[str, Any]], body: int = 600) -> list[dict[str, Any]]:
    """The exchanges in short: what was sent and what came back, without headers (they are in the file)."""
    out = []
    for r in records:
        request = r.get("request") or {}
        b: dict[str, Any] = {"no": r.get("no"), "origin": r.get("origin"), "operation": r.get("operation"),
                             "request": {"url": request.get("url"), "body": shorten(request.get("body"), body),
                                         "timeout_s": request.get("timeout_s"), "proxy": request.get("proxy")},
                             "duration_ms": r.get("duration_ms")}
        if r.get("response"):
            b["response"] = {"status": r["response"].get("status"),
                             "body": shorten(r["response"].get("body"), body * 2)}
        if r.get("error"):
            e = r["error"]
            b["error"] = {"phase": e.get("phase"), "kind": e.get("kind"), "chain": (e.get("chain") or [])[-3:],
                          "likely_causes": e.get("likely_causes") or []}
        if r.get("business") is not None:
            b["business"] = r["business"]
        out.append(b)
    return out


def full(records: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """The exchanges with headers; bodies shaped to STM_TRACE_BODY_CHARS (default 2000)."""
    limit = limit or int(env_setting("STM_TRACE_BODY_CHARS", "STM_IZ_GOVDE", "2000") or 2000)
    out = []
    for r in records:
        r = dict(r)
        for side in ("request", "response"):
            if isinstance(r.get(side), dict):
                r[side] = {**r[side], "body": shorten(r[side].get("body"), limit)}
        out.append(r)
    return out


def write_file(data: Any, title: str) -> str | None:
    try:
        folder = Path(env_setting("STM_TRACE_DIR", "STM_IZ_DIR") or home_dir() / "traces")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")[:40] or "trace"
        path = folder / f"{stamp}-{safe}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        for old in sorted(folder.glob("*.json"))[:-FILES_KEPT]:
            old.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return None
