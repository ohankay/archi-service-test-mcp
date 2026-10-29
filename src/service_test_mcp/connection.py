"""Connection check: tries every way (route) to a system's address and says which one works and where others break.

Routes tried:
* the route the MCP uses now (system definition / HTTPS_PROXY / Windows fixed proxy),
* direct, without any proxy,
* each proxy Windows would use for this address (the browser's choice, from the PAC script) or knows of.

Each route is walked step by step - DNS, TCP, TLS, one HTTP request - with timings, so "login timed out" becomes
"direct: TCP ok, TLS ok, HTTP 404 in 80 ms; via proxy:8080: TLS did not complete -> write 'proxy: none'".
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from . import winproxy
from .trace import classify


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _dns(host: str, port: int) -> tuple[dict[str, Any], bool]:
    started = time.perf_counter()
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        return {"step": "DNS", "status": "ok", "duration_ms": _ms(started),
                "detail": f"{host} -> {', '.join(ips)}"}, True
    except OSError as exc:
        return {"step": "DNS", "status": "error", "duration_ms": _ms(started),
                "detail": f"{host} could not be resolved: {exc}",
                "likely_causes": ["VPN is off or the company DNS is not used", "The address is misspelled"]}, False


def _tcp(host: str, port: int, seconds: float, is_proxy: bool) -> tuple[dict[str, Any], bool]:
    started = time.perf_counter()
    what = f"{host}:{port}" + (" (proxy)" if is_proxy else "")
    try:
        with socket.create_connection((host, port), timeout=seconds):
            pass
        return {"step": "TCP connection", "status": "ok", "duration_ms": _ms(started), "detail": f"{what} opened"}, True
    except OSError as exc:
        return {"step": "TCP connection", "status": "error", "duration_ms": _ms(started),
                "detail": f"{what} did not open in {seconds:g} s: {type(exc).__name__}: {exc}",
                "likely_causes": ["Wrong or unreachable proxy address"] if is_proxy else
                ["No network path from this computer to the server (VPN / company network)",
                 "The company needs a proxy", "Firewall"]}, False


def _tls(host: str, port: int, seconds: float, verify: Any, base: str) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
    if verify is False:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=seconds) as raw, \
                ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}

            def name(key: str) -> str:
                return ", ".join("=".join(x) for rdn in cert.get(key, ()) for x in rdn)

            return {"step": "TLS handshake", "status": "ok", "duration_ms": _ms(started),
                    "detail": f"{tls.version()}" + (f"; certificate: {name('subject')}; issuer: {name('issuer')}; "
                                                   f"expires: {cert.get('notAfter')}" if cert else
                                                   " (verification off)")}
    except (OSError, ssl.SSLError) as exc:
        timed_out = "timed out" in str(exc)
        return {"step": "TLS handshake", "status": "error", "duration_ms": _ms(started),
                "detail": f"{type(exc).__name__}: {exc}"
                + (f" (TCP opened, TLS did not complete in {seconds:g} s)" if timed_out else ""),
                "likely_causes": classify(exc, base, seconds, None).get("likely_causes", []) if timed_out else
                ["The company's certificate authority is not known: write verify_tls: <company CA .pem path> in "
                 "the environment", "Only on test, for a while: verify_tls: false"]}


def _http(base: str, proxy: str | None, verify: Any, seconds: float) -> dict[str, Any]:
    """Any HTTP answer (even 404 / 405) means the server is reachable this way."""
    started = time.perf_counter()
    with requests.Session() as s:
        s.trust_env = False                       # exactly this route, nothing from the environment
        s.verify = verify
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        try:
            resp = s.get(base, timeout=seconds, allow_redirects=False)
            return {"step": "HTTP request", "status": "ok", "duration_ms": _ms(started),
                    "detail": f"GET {base} -> HTTP {resp.status_code} {resp.reason} (the server answers; the status "
                              "code does not matter here)",
                    "response_headers": dict(resp.headers), "response_start": resp.text[:500]}
        except Exception as exc:  # noqa: BLE001 - this is what we are here to show
            detail = classify(exc, base, seconds, proxy)
            return {"step": "HTTP request", "status": "error", "duration_ms": _ms(started), "detail": detail["phase"],
                    "chain": detail["chain"], "likely_causes": detail.get("likely_causes", [])}


def _route(name: str, base: str, proxy: str | None, verify: Any, seconds: float, http_seconds: float) -> dict[str, Any]:
    parts = urlsplit(base)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    steps: list[dict[str, Any]] = []
    if proxy:
        p = urlsplit(proxy)
        step, ok = _tcp(p.hostname or "", p.port or 8080, seconds, True)
        steps.append(step)
    else:
        step, ok = _dns(host, port)
        steps.append(step)
        if ok:
            step, ok = _tcp(host, port, seconds, False)
            steps.append(step)
        if ok and parts.scheme == "https":
            step = _tls(host, port, seconds, verify, base)
            steps.append(step)
            ok = step["status"] == "ok"
    if ok:
        steps.append(_http(base, proxy, verify, http_seconds))
    broken = next((s for s in steps if s["status"] == "error"), None)
    return {"route": name, "proxy": proxy, "steps": steps, "works": broken is None,
            "result": "works" if broken is None else f"broke at '{broken['step']}': {broken['detail']}"}


def _pac(pac: str) -> dict[str, Any]:
    """The PAC file itself, fetched directly (a proxy may not know the short intranet name)."""
    out: dict[str, Any] = {}
    try:
        with requests.Session() as s:
            s.trust_env = False
            text = s.get(pac, timeout=5).text
        out["pac_proxies"] = sorted(set(re.findall(r"PROXY\s+([\w.\-]+:\d+)", text)))
        out["pac_start"] = text[:4000]
    except Exception as exc:  # noqa: BLE001 - shown, not fatal
        out["pac_error"] = f"could not be downloaded directly: {type(exc).__name__}: {str(exc)[:300]}"
    return out


def check(engine: Any, service: str, environment: str | None, tcp_seconds: float = 5.0) -> dict[str, Any]:
    svc, env = engine.resolve(service, environment)
    base = svc.value_for("base_url", env.name) or ""
    parts = urlsplit(base)
    session, _ = engine.rest.session(svc, env)
    settings = session.merge_environment_settings(base, {}, None, session.verify, None)
    proxies = settings.get("proxies") or {}
    proxy = proxies.get(parts.scheme) or proxies.get("all")
    verify = settings.get("verify")
    timeout = svc.timeout_s or env.timeout_s
    http_seconds = min(timeout, 15)
    if env.proxy and str(env.proxy).lower() == "auto":
        source = f"system definition: proxy: auto -> {getattr(session, 'stm_proxy_decision', None)}"
    elif env.proxy:
        source = f"system definition: proxy: {env.proxy}"
    else:
        source = "HTTPS_PROXY environment variable or the Windows fixed proxy" if proxy else "-"
    out: dict[str, Any] = {"service": svc.name, "environment": env.name, "address": base,
                           "settings": {"timeout_s": timeout, "proxy": proxy or "none (direct connection)",
                                        "proxy_source": source, "verify_tls": verify},
                           "environment_variables": {k: os.environ[k] for k in
                                                     ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                                      "NO_PROXY", "no_proxy", "REQUESTS_CA_BUNDLE")
                                                     if os.environ.get(k)}}

    candidates: list[tuple[str, str | None]] = [(f"the MCP's current route ({proxy or 'direct'})", proxy)]
    win = winproxy.settings()
    if win is not None:
        if win.get("pac_url"):
            win.update(_pac(win["pac_url"]))
        decision = winproxy.decision(base, win.get("pac_url"))
        win["decision_for_this_address"] = decision
        out["windows_proxy"] = win
        for p in (decision or {}).get("proxies") or []:
            candidates.append((f"Windows / browser decision: proxy {p}", p))
        if win.get("fixed_proxy"):
            for p in winproxy.proxy_urls(win["fixed_proxy"]):
                candidates.append((f"Windows fixed proxy {p}", p))
    if proxy:
        candidates.append(("direct (no proxy)", None))

    routes, seen = [], set()
    for name, p in candidates:
        if p in seen:
            continue
        seen.add(p)
        routes.append(_route(name, base, p, verify, tcp_seconds, http_seconds))
    out["routes"] = routes
    current = routes[0]
    working = [r for r in routes[1:] if r["works"]]
    if current["works"]:
        out["result"] = "reachable"
    else:
        out["result"] = f"not reachable: {current['result']}"
        if working:
            w = working[0]
            setting = "proxy: none" if w["proxy"] is None else f"proxy: {w['proxy']}"
            out["suggestion"] = (f"'{w['route']}' works. Write '{setting}' in the '{env.name}' environment of the "
                                 "system definition" + (" (or 'proxy: auto' to choose like the browser)"
                                                        if w["route"].startswith("Windows / browser") else "")
                                 + " and try again.")
        else:
            out["suggestion"] = ("No route worked. Open the address in a browser on the same computer: if it opens, "
                                 "compare the browser's proxy (settings / PAC); if not, check the VPN, a firewall "
                                 "permission or the server side.")
    out["success"] = current["works"]
    return out


def _cut(text: Any, n: int) -> Any:
    return text if not isinstance(text, str) or len(text) <= n else text[:n] + "..."


def summary(r: dict[str, Any]) -> dict[str, Any]:
    """The check in a few hundred characters per route - what an agent can show; details go to the file."""
    routes = []
    for route in r.get("routes") or []:
        steps = []
        for s in route["steps"]:
            short = {"step": s["step"], "status": s["status"], "duration_ms": s["duration_ms"],
                     "detail": _cut(s["detail"], 300)}
            if s["status"] == "error":
                short["likely_causes"] = [_cut(c, 200) for c in s.get("likely_causes") or []][:3]
                if s.get("chain"):
                    short["chain_end"] = _cut(s["chain"][-1], 200)
            steps.append(short)
        routes.append({"route": route["route"], "proxy": route["proxy"], "works": route["works"], "steps": steps})
    out: dict[str, Any] = {k: r[k] for k in ("service", "environment", "address", "result", "suggestion", "success",
                                             "settings") if k in r}
    out["routes"] = routes
    win = r.get("windows_proxy")
    if win:
        w = {k: _cut(win[k], 300) for k in ("fixed_proxy_enabled", "fixed_proxy", "proxy_bypass", "pac_url",
                                             "pac_error", "error") if k in win}
        if win.get("pac_proxies") is not None:
            w["pac_proxies"] = win["pac_proxies"][:10]
        decision = win.get("decision_for_this_address")
        if decision:
            w["decision_for_this_address"] = decision.get("error") or decision.get("raw")
        out["windows_proxy"] = w
    return out
