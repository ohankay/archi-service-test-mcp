"""Trace (iz): every HTTP exchange made while a tool runs, so nothing the server does is hidden.

For each exchange the trace keeps what was really sent and received: method, full URL, headers (including the ones
the session adds), body, timeout, proxy, TLS verification, status, response headers and body, redirects, time, the
business result and - when it failed - what kind of failure it was (DNS, TCP connect, TLS, proxy, no response in
time ...) with the whole cause chain.

The tool result gets:

* ``akis``      one readable line per exchange ("2) [giriş 2/4] POST .../InitialLogin -> ZAMAN AŞIMI ...")
* ``iz``        the exchanges (long bodies shortened to STM_IZ_GOVDE characters, default 4000)
* ``iz_dosyasi`` the complete trace as JSON in %USERPROFILE%\\.service-test-mcp\\izler\\ (open it in VS Code)
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .config import home_dir

_aktif: ContextVar[list[dict[str, Any]] | None] = ContextVar("stm_iz", default=None)
DOSYA_SAYISI = 200


@contextlib.contextmanager
def topla() -> Iterator[list[dict[str, Any]]]:
    kayitlar: list[dict[str, Any]] = []
    token = _aktif.set(kayitlar)
    try:
        yield kayitlar
    finally:
        _aktif.reset(token)


def ekle(kayit: dict[str, Any]) -> dict[str, Any]:
    kayitlar = _aktif.get()
    if kayitlar is not None:
        kayit = {"no": len(kayitlar) + 1, **kayit}
        kayitlar.append(kayit)
    return kayit


def son() -> dict[str, Any] | None:
    kayitlar = _aktif.get()
    return kayitlar[-1] if kayitlar else None


# -- errors ---------------------------------------------------------------------------------------------------------
def hata_zinciri(exc: BaseException) -> list[str]:
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


def siniflandir(exc: BaseException, url: str, timeout: Any, proxy: str | None) -> dict[str, Any]:
    """A request exception -> what happened, in which phase, and what usually causes it."""
    import requests

    zincir = hata_zinciri(exc)
    text = " | ".join(zincir)
    host = re.sub(r"^\w+://", "", url).split("/")[0]
    out: dict[str, Any] = {"tur": type(exc).__name__, "zincir": zincir}
    if isinstance(exc, requests.exceptions.ConnectTimeout) or "ConnectTimeout" in text:
        out["asama"] = (f"bağlantı kurulamadı: {host} adresine {timeout} sn içinde TCP/TLS bağlantısı açılamadı "
                        f"(proxy: {proxy or 'yok'})")
        out["olasi_nedenler"] = ["Bu bilgisayardan sunucuya ağ yolu yok (VPN kapalı / kurum ağı dışında)",
                                 "Kurum proxy'si gerekiyor ama kullanılmıyor (sistem tanımında 'proxy' veya "
                                 "HTTPS_PROXY)", "Güvenlik duvarı bu bağlantıyı düşürüyor"]
    elif isinstance(exc, requests.exceptions.ReadTimeout) or "ReadTimeout" in text or "Read timed out" in text:
        out["asama"] = f"bağlantı kuruldu, istek gitti; sunucu {timeout} sn içinde yanıt vermedi"
        out["olasi_nedenler"] = ["Sunucu yavaş veya istek takıldı (zaman_asimi_sn artırılabilir)",
                                 "İstek gövdesi sunucunun beklediği gibi değil ve sunucu yanıtlamıyor"]
    elif isinstance(exc, requests.exceptions.ProxyError) or "ProxyError" in text:
        out["asama"] = f"proxy hatası ({proxy})"
        out["olasi_nedenler"] = ["Proxy adresi yanlış veya erişilemiyor", "Proxy kimlik doğrulaması istiyor"]
    elif isinstance(exc, requests.exceptions.SSLError) or "SSLError" in text or "CERTIFICATE" in text.upper():
        out["asama"] = "TLS (sertifika) doğrulaması başarısız"
        out["olasi_nedenler"] = ["Kurumun kendi sertifika otoritesi tanınmıyor: ortamda tls_dogrula: <CA .pem yolu>",
                                 "Araya giren bir proxy sertifikayı değiştiriyor"]
    elif "NameResolution" in text or "getaddrinfo" in text or "Name or service not known" in text:
        out["asama"] = f"DNS: '{host}' adı çözülemedi"
        out["olasi_nedenler"] = ["Bu bilgisayarın DNS'i bu adı bilmiyor (VPN / kurum DNS'i gerekiyor)",
                                 "Adres yanlış yazılmış"]
    elif "Connection refused" in text or "ConnectionRefused" in text or "WinError 10061" in text:
        out["asama"] = f"bağlantı reddedildi: {host} bu portta dinlemiyor"
        out["olasi_nedenler"] = ["Adres veya port yanlış", "Servis kapalı"]
    elif "RemoteDisconnected" in text or "Connection aborted" in text or "reset" in text.lower():
        out["asama"] = "bağlantı karşı taraftan kapatıldı"
        out["olasi_nedenler"] = ["Sunucu veya araya giren bir cihaz isteği reddetti"]
    else:
        out["asama"] = "istek gönderilemedi"
    return out


# -- views ------------------------------------------------------------------------------------------------------------
def _kisalt(value: Any, limit: int) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return text[:limit] + f"... (+{len(text) - limit} karakter; tamamı iz dosyasında)"


def satir(k: dict[str, Any]) -> str:
    istek = k.get("istek") or {}
    yanit = k.get("yanit") or {}
    url = istek.get("url") or ""
    head = f"{k.get('no')}) [{k.get('kaynak')}] {istek.get('metod', '')} {url}"
    if k.get("hata"):
        h = k["hata"]
        return f"{head} -> HATA: {h.get('asama')} [{h.get('tur')}], {k.get('sure_ms')} ms"
    durum = yanit.get("durum")
    text = f"{head} -> HTTP {durum}, {k.get('sure_ms')} ms"
    sonuc = k.get("is_sonucu")
    if sonuc is not None:
        text += ", iş sonucu: " + ("başarılı" if sonuc.get("success") else
                                   f"başarısız ({'; '.join(map(str, sonuc.get('errors') or [])) or '-'})")
    if k.get("alinan"):
        text += f", alınan: {', '.join(k['alinan'])}"
    return text


def kisa(kayitlar: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    limit = limit or int(os.environ.get("STM_IZ_GOVDE", "4000"))
    out = []
    for k in kayitlar:
        k = dict(k)
        if isinstance(k.get("istek"), dict):
            k["istek"] = {**k["istek"], "govde": _kisalt(k["istek"].get("govde"), limit)}
        if isinstance(k.get("yanit"), dict):
            k["yanit"] = {**k["yanit"], "govde": _kisalt(k["yanit"].get("govde"), limit)}
        out.append(k)
    return out


def dosyaya_yaz(kayitlar: list[dict[str, Any]], baslik: str) -> str | None:
    try:
        folder = Path(os.environ.get("STM_IZ_DIR") or home_dir() / "izler")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", baslik).strip("-")[:40] or "iz"
        path = folder / f"{stamp}-{safe}.json"
        path.write_text(json.dumps(kayitlar, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        files = sorted(folder.glob("*.json"))
        for old in files[:-DOSYA_SAYISI]:
            old.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return None
