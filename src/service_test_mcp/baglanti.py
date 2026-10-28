"""Connection check: walks the way to a system's address step by step and says where it breaks.

DNS -> proxy in use -> TCP connect -> TLS handshake (certificate) -> one HTTP request. Each step reports what it did,
how long it took and what it found, so "giriş zaman aşımına uğradı" becomes "DNS ok, TCP to 10.1.2.3:443 did not
open in 5 s, no proxy configured -> VPN / proxy".
"""

from __future__ import annotations

import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

from .iz import siniflandir


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def kontrol_et(engine: Any, service: str, environment: str | None, tcp_sn: float = 5.0) -> dict[str, Any]:
    svc, env = engine.resolve(service, environment)
    base = svc.value_for("base_url", env.name) or ""
    parts = urlsplit(base)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    session, _ = engine.rest.session(svc, env)
    settings = session.merge_environment_settings(base, {}, None, session.verify, None)
    proxy = (settings.get("proxies") or {}).get(parts.scheme) or (settings.get("proxies") or {}).get("all")
    out: dict[str, Any] = {"servis": svc.name, "ortam": env.name, "adres": base, "sunucu": host, "port": port,
                           "ayarlar": {"zaman_asimi_sn": svc.timeout_s or env.timeout_s,
                                       "proxy": proxy or "yok (doğrudan bağlantı)",
                                       "proxy_kaynagi": "sistem tanımı" if env.proxy else
                                       ("HTTPS_PROXY / HTTP_PROXY ortam değişkeni" if proxy else "-"),
                                       "tls_dogrulama": settings.get("verify")},
                           "adimlar": []}
    adimlar: list[dict[str, Any]] = out["adimlar"]

    # 1. DNS
    started = time.perf_counter()
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        adimlar.append({"adim": "DNS", "durum": "tamam", "sure_ms": _ms(started),
                        "detay": f"{host} -> {', '.join(ips)}"})
    except OSError as exc:
        ips = []
        adimlar.append({"adim": "DNS", "durum": "hata", "sure_ms": _ms(started),
                        "detay": f"{host} çözülemedi: {exc}",
                        "olasi_nedenler": ["VPN kapalı ya da kurum DNS'i kullanılmıyor", "Adres yanlış yazılmış"]
                        + (["Proxy kullanılıyorsa bu normal olabilir: adı proxy çözer"] if proxy else [])})

    # 2. TCP (direct, or to the proxy)
    target = urlsplit(proxy) if proxy else None
    tcp_host, tcp_port = (target.hostname, target.port or 8080) if target else (host, port)
    tcp_ok = False
    if tcp_host and (ips or target):
        started = time.perf_counter()
        try:
            with socket.create_connection((tcp_host, tcp_port), timeout=tcp_sn):
                tcp_ok = True
            adimlar.append({"adim": "TCP bağlantısı", "durum": "tamam", "sure_ms": _ms(started),
                            "detay": f"{tcp_host}:{tcp_port} açıldı" + (" (proxy)" if target else "")})
        except OSError as exc:
            adimlar.append({"adim": "TCP bağlantısı", "durum": "hata", "sure_ms": _ms(started),
                            "detay": f"{tcp_host}:{tcp_port} {tcp_sn:g} sn içinde açılamadı"
                            + (" (proxy)" if target else "") + f": {type(exc).__name__}: {exc}",
                            "olasi_nedenler": (["Proxy adresi yanlış ya da erişilemiyor"] if target else
                                               ["Bu bilgisayardan sunucuya ağ yolu yok (VPN / kurum ağı)",
                                                "Kurum proxy gerektiriyor: sistem tanımında ortama 'proxy' yazın "
                                                "ya da HTTPS_PROXY tanımlayın", "Güvenlik duvarı"])})

    # 3. TLS (only direct https; through a proxy the HTTP step covers it)
    if tcp_ok and not target and parts.scheme == "https":
        started = time.perf_counter()
        verify = settings.get("verify")
        ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
        if verify is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((host, port), timeout=tcp_sn) as raw, \
                    ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                name = lambda key: ", ".join("=".join(x) for rdn in cert.get(key, ()) for x in rdn)  # noqa: E731
                adimlar.append({"adim": "TLS el sıkışma", "durum": "tamam", "sure_ms": _ms(started),
                                "detay": f"{tls.version()}" + (f"; sertifika: {name('subject')}; veren: "
                                                              f"{name('issuer')}; bitiş: {cert.get('notAfter')}"
                                                              if cert else " (doğrulama kapalı)")})
        except (OSError, ssl.SSLError) as exc:
            adimlar.append({"adim": "TLS el sıkışma", "durum": "hata", "sure_ms": _ms(started),
                            "detay": f"{type(exc).__name__}: {exc}",
                            "olasi_nedenler": ["Kurumun sertifika otoritesi tanınmıyor: ortama "
                                               "tls_dogrula: <kurum CA .pem yolu> yazın",
                                               "Test ortamında geçici olarak tls_dogrula: false"]})

    # 4. HTTP: any HTTP answer (even 404 / 405) means the server is reachable
    started = time.perf_counter()
    try:
        resp = session.get(base, timeout=min(svc.timeout_s or env.timeout_s, 15), allow_redirects=False)
        adimlar.append({"adim": "HTTP isteği", "durum": "tamam", "sure_ms": _ms(started),
                        "detay": f"GET {base} -> HTTP {resp.status_code} {resp.reason} (sunucu yanıt veriyor; "
                                 "durum kodu burada önemli değil)",
                        "yanit_basliklari": dict(resp.headers), "yanit_basi": resp.text[:500]})
    except Exception as exc:  # noqa: BLE001 - this is what we are here to show
        detail = siniflandir(exc, base, min(svc.timeout_s or env.timeout_s, 15), proxy)
        adimlar.append({"adim": "HTTP isteği", "durum": "hata", "sure_ms": _ms(started),
                        "detay": detail["asama"], "zincir": detail["zincir"],
                        "olasi_nedenler": detail.get("olasi_nedenler", [])})

    broken = next((a for a in adimlar if a["durum"] == "hata"), None)
    out["sonuc"] = "erişilebilir" if broken is None else f"erişilemiyor: '{broken['adim']}' adımında kesildi"
    out["success"] = broken is None
    return out

