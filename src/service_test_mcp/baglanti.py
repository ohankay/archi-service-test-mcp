"""Connection check: tries every way to a system's address and says which one works and where the others break.

Ways (yollar) tried:
* the way the MCP uses now (system definition / HTTPS_PROXY / Windows fixed proxy),
* direct, without any proxy,
* each proxy Windows would use for this address (the browser's choice, from the PAC script) or knows of.

Each way is walked step by step - DNS, TCP, TLS, one HTTP request - with timings, so "giriş zaman aşımına uğradı"
becomes "direct: TCP ok, TLS ok, HTTP 404 in 80 ms; via ktproxy:8080: TLS did not complete -> write 'proxy: yok'".
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
from .iz import siniflandir


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _dns(host: str, port: int, proxy: str | None) -> tuple[dict[str, Any], bool]:
    started = time.perf_counter()
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        return {"adim": "DNS", "durum": "tamam", "sure_ms": _ms(started),
                "detay": f"{host} -> {', '.join(ips)}"}, True
    except OSError as exc:
        return {"adim": "DNS", "durum": "hata", "sure_ms": _ms(started), "detay": f"{host} çözülemedi: {exc}",
                "olasi_nedenler": ["VPN kapalı ya da kurum DNS'i kullanılmıyor", "Adres yanlış yazılmış"]
                + (["Proxy kullanılıyorsa bu normal olabilir: adı proxy çözer"] if proxy else [])}, False


def _tcp(host: str, port: int, sn: float, is_proxy: bool) -> tuple[dict[str, Any], bool]:
    started = time.perf_counter()
    what = f"{host}:{port}" + (" (proxy)" if is_proxy else "")
    try:
        with socket.create_connection((host, port), timeout=sn):
            pass
        return {"adim": "TCP bağlantısı", "durum": "tamam", "sure_ms": _ms(started), "detay": f"{what} açıldı"}, True
    except OSError as exc:
        return {"adim": "TCP bağlantısı", "durum": "hata", "sure_ms": _ms(started),
                "detay": f"{what} {sn:g} sn içinde açılamadı: {type(exc).__name__}: {exc}",
                "olasi_nedenler": ["Proxy adresi yanlış ya da erişilemiyor"] if is_proxy else
                ["Bu bilgisayardan sunucuya ağ yolu yok (VPN / kurum ağı)", "Kurum proxy gerektiriyor",
                 "Güvenlik duvarı"]}, False


def _tls(host: str, port: int, sn: float, verify: Any, base: str) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
    if verify is False:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=sn) as raw, \
                ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}

            def name(key: str) -> str:
                return ", ".join("=".join(x) for rdn in cert.get(key, ()) for x in rdn)

            return {"adim": "TLS el sıkışma", "durum": "tamam", "sure_ms": _ms(started),
                    "detay": f"{tls.version()}" + (f"; sertifika: {name('subject')}; veren: {name('issuer')}; "
                                                   f"bitiş: {cert.get('notAfter')}" if cert else
                                                   " (doğrulama kapalı)")}
    except (OSError, ssl.SSLError) as exc:
        timed_out = "timed out" in str(exc)
        return {"adim": "TLS el sıkışma", "durum": "hata", "sure_ms": _ms(started),
                "detay": f"{type(exc).__name__}: {exc}"
                + (f" (TCP açıldı, TLS {sn:g} sn içinde tamamlanmadı)" if timed_out else ""),
                "olasi_nedenler": siniflandir(exc, base, sn, None).get("olasi_nedenler", []) if timed_out else
                ["Kurumun sertifika otoritesi tanınmıyor: ortama tls_dogrula: <kurum CA .pem yolu> yazın",
                 "Test ortamında geçici olarak tls_dogrula: false"]}


def _http(base: str, proxy: str | None, verify: Any, sn: float) -> dict[str, Any]:
    """Any HTTP answer (even 404 / 405) means the server is reachable this way."""
    started = time.perf_counter()
    with requests.Session() as s:
        s.trust_env = False                       # exactly this way, nothing from the environment
        s.verify = verify
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        try:
            resp = s.get(base, timeout=sn, allow_redirects=False)
            return {"adim": "HTTP isteği", "durum": "tamam", "sure_ms": _ms(started),
                    "detay": f"GET {base} -> HTTP {resp.status_code} {resp.reason} (sunucu yanıt veriyor; durum "
                             "kodu burada önemli değil)",
                    "yanit_basliklari": dict(resp.headers), "yanit_basi": resp.text[:500]}
        except Exception as exc:  # noqa: BLE001 - this is what we are here to show
            detail = siniflandir(exc, base, sn, proxy)
            return {"adim": "HTTP isteği", "durum": "hata", "sure_ms": _ms(started), "detay": detail["asama"],
                    "zincir": detail["zincir"], "olasi_nedenler": detail.get("olasi_nedenler", [])}


def _yol(ad: str, base: str, proxy: str | None, verify: Any, sn: float, http_sn: float) -> dict[str, Any]:
    parts = urlsplit(base)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    adimlar: list[dict[str, Any]] = []
    if proxy:
        p = urlsplit(proxy)
        step, ok = _tcp(p.hostname or "", p.port or 8080, sn, True)
        adimlar.append(step)
    else:
        step, ok = _dns(host, port, None)
        adimlar.append(step)
        if ok:
            step, ok = _tcp(host, port, sn, False)
            adimlar.append(step)
        if ok and parts.scheme == "https":
            step = _tls(host, port, sn, verify, base)
            adimlar.append(step)
            ok = step["durum"] == "tamam"
    if ok:
        adimlar.append(_http(base, proxy, verify, http_sn))
    broken = next((a for a in adimlar if a["durum"] == "hata"), None)
    return {"yol": ad, "proxy": proxy, "adimlar": adimlar, "calisiyor": broken is None,
            "sonuc": "çalışıyor" if broken is None else f"'{broken['adim']}' adımında kesildi: {broken['detay']}"}


def _pac(pac: str) -> dict[str, Any]:
    """The PAC file itself, fetched directly (a proxy may not know the short intranet name)."""
    out: dict[str, Any] = {}
    try:
        with requests.Session() as s:
            s.trust_env = False
            text = s.get(pac, timeout=5).text
        out["pac_proxyleri"] = sorted(set(re.findall(r"PROXY\s+([\w.\-]+:\d+)", text)))
        out["pac_basi"] = text[:4000]
    except Exception as exc:  # noqa: BLE001 - shown, not fatal
        out["pac_okunamadi"] = f"doğrudan indirilemedi: {type(exc).__name__}: {str(exc)[:300]}"
    return out


def kontrol_et(engine: Any, service: str, environment: str | None, tcp_sn: float = 5.0) -> dict[str, Any]:
    svc, env = engine.resolve(service, environment)
    base = svc.value_for("base_url", env.name) or ""
    parts = urlsplit(base)
    session, _ = engine.rest.session(svc, env)
    settings = session.merge_environment_settings(base, {}, None, session.verify, None)
    proxies = settings.get("proxies") or {}
    proxy = proxies.get(parts.scheme) or proxies.get("all")
    verify = settings.get("verify")
    timeout = svc.timeout_s or env.timeout_s
    http_sn = min(timeout, 15)
    if env.proxy and str(env.proxy).lower() in ("auto", "otomatik"):
        kaynak = f"sistem tanımı: proxy: otomatik -> {getattr(session, 'stm_proxy_karari', None)}"
    elif env.proxy:
        kaynak = f"sistem tanımı: proxy: {env.proxy}"
    else:
        kaynak = "HTTPS_PROXY ortam değişkeni ya da Windows sabit proxy ayarı" if proxy else "-"
    out: dict[str, Any] = {"servis": svc.name, "ortam": env.name, "adres": base,
                           "ayarlar": {"zaman_asimi_sn": timeout, "proxy": proxy or "yok (doğrudan bağlantı)",
                                       "proxy_kaynagi": kaynak, "tls_dogrulama": verify},
                           "ortam_degiskenleri": {k: os.environ[k] for k in
                                                  ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                                   "NO_PROXY", "no_proxy", "REQUESTS_CA_BUNDLE") if os.environ.get(k)}}

    adaylar: list[tuple[str, str | None]] = [(f"MCP'nin şu anki yolu ({proxy or 'doğrudan'})", proxy)]
    win = winproxy.ayarlar()
    if win is not None:
        if win.get("pac_adresi"):
            win.update(_pac(win["pac_adresi"]))
        karar = winproxy.karar(base, win.get("pac_adresi"))
        win["bu_adres_icin_windows_karari"] = karar
        out["windows_proxy"] = win
        for p in (karar or {}).get("proxyler") or []:
            adaylar.append((f"Windows/tarayıcı kararı: proxy {p}", p))
        if win.get("sabit_proxy"):
            for p in winproxy.proxy_adresleri(win["sabit_proxy"]):
                adaylar.append((f"Windows sabit proxy {p}", p))
    if proxy:
        adaylar.append(("doğrudan (proxy'siz)", None))

    yollar, seen = [], set()
    for ad, p in adaylar:
        if p in seen:
            continue
        seen.add(p)
        yollar.append(_yol(ad, base, p, verify, tcp_sn, http_sn))
    out["yollar"] = yollar
    current = yollar[0]
    working = [y for y in yollar[1:] if y["calisiyor"]]
    if current["calisiyor"]:
        out["sonuc"] = "erişilebilir"
    else:
        out["sonuc"] = f"erişilemiyor: {current['sonuc']}"
        if working:
            w = working[0]
            line = "proxy: yok" if w["proxy"] is None else f"proxy: {w['proxy']}"
            out["oneri"] = (f"'{w['yol']}' çalışıyor. Sistem tanımında '{env.name}' ortamına '{line}' yazın"
                            + (" (ya da tarayıcı gibi seçmesi için 'proxy: otomatik')"
                               if w["yol"].startswith("Windows/tarayıcı") else "") + " ve tekrar deneyin.")
        else:
            out["oneri"] = ("Denenen hiçbir yol çalışmadı. Adresi aynı bilgisayarda tarayıcıda açmayı deneyin: "
                            "açılıyorsa tarayıcının proxy'sini (ayarlar / PAC) karşılaştırın; açılmıyorsa VPN, "
                            "güvenlik duvarı izni ya da sunucu tarafını kontrol edin.")
    out["success"] = current["calisiyor"]
    return out


def _kes(text: Any, n: int) -> Any:
    return text if not isinstance(text, str) or len(text) <= n else text[:n] + "..."


def ozet(r: dict[str, Any]) -> dict[str, Any]:
    """The check in a few hundred characters per way - what an agent can show; details go to the file."""
    yollar = []
    for y in r.get("yollar") or []:
        adimlar = []
        for a in y["adimlar"]:
            k = {"adim": a["adim"], "durum": a["durum"], "sure_ms": a["sure_ms"], "detay": _kes(a["detay"], 300)}
            if a["durum"] == "hata":
                k["olasi_nedenler"] = [_kes(n, 200) for n in a.get("olasi_nedenler") or []][:3]
                if a.get("zincir"):
                    k["zincir_sonu"] = _kes(a["zincir"][-1], 200)
            adimlar.append(k)
        yollar.append({"yol": y["yol"], "proxy": y["proxy"], "calisiyor": y["calisiyor"], "adimlar": adimlar})
    out: dict[str, Any] = {k: r[k] for k in ("servis", "ortam", "adres", "sonuc", "oneri", "success", "ayarlar")
                           if k in r}
    out["yollar"] = yollar
    win = r.get("windows_proxy")
    if win:
        w = {k: _kes(win[k], 300) for k in ("sabit_proxy_acik", "sabit_proxy", "proxy_disi", "pac_adresi",
                                             "pac_okunamadi", "hata") if k in win}
        if win.get("pac_proxyleri") is not None:
            w["pac_proxyleri"] = win["pac_proxyleri"][:10]
        karar = win.get("bu_adres_icin_windows_karari")
        if karar:
            w["bu_adres_icin_windows_karari"] = karar.get("hata") or karar.get("ham")
        out["windows_proxy"] = w
    return out
