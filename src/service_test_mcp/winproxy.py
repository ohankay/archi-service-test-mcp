"""What Windows (and so the browser) would use to reach a URL: a proxy or a direct connection.

Python's requests reads only HTTPS_PROXY and the fixed proxy in the Windows settings; it does not run the PAC
(automatic configuration) script that browsers use. Here Windows itself evaluates it (WinHTTP), so the answer is the
same as the browser's: "DIRECT" or "PROXY host:port".
"""

from __future__ import annotations

import re
import sys
from typing import Any

HATALAR = {12180: "otomatik algılama (WPAD) proxy betiği bulamadı",
           12167: "PAC dosyası indirilemedi",
           12166: "PAC dosyası çalıştırılırken hata oluştu",
           12178: "Windows otomatik proxy hizmeti hata verdi",
           12005: "adres geçersiz"}


def ayarlar() -> dict[str, Any] | None:
    """Proxy settings of the current Windows user (Internet Options). None outside Windows."""
    if sys.platform != "win32":
        return None
    import winreg

    out: dict[str, Any] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            for name, label in (("ProxyEnable", "sabit_proxy_acik"), ("ProxyServer", "sabit_proxy"),
                                ("ProxyOverride", "proxy_disi"), ("AutoConfigURL", "pac_adresi")):
                try:
                    out[label] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
    except OSError as exc:
        return {"hata": str(exc)}
    if "sabit_proxy_acik" in out:
        out["sabit_proxy_acik"] = bool(out["sabit_proxy_acik"])
    return out


def proxy_adresleri(text: str | None) -> list[str]:
    """'host:8080;https=h2:8443' / 'PROXY a:1; DIRECT' -> ['http://host:8080', 'http://h2:8443']"""
    out: list[str] = []
    for part in re.split(r"[;\s]+", text or ""):
        part = part.strip()
        if not part or part.upper() in ("PROXY", "DIRECT", "HTTP", "HTTPS"):
            continue
        part = re.sub(r"^(https?|ftp|socks)=", "", part, flags=re.I)
        if ":" not in part:
            continue
        url = part if "://" in part else "http://" + part
        if url not in out:
            out.append(url)
    return out


def karar(url: str, pac_adresi: str | None = None) -> dict[str, Any] | None:
    """Ask Windows (WinHTTP) which way it would reach url. {'yol': 'doğrudan'|'proxy', 'proxyler': [...]}"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Secenek(ctypes.Structure):
        _fields_ = [("dwFlags", wintypes.DWORD), ("dwAutoDetectFlags", wintypes.DWORD),
                    ("lpszAutoConfigUrl", wintypes.LPCWSTR), ("lpvReserved", ctypes.c_void_p),
                    ("dwReserved", wintypes.DWORD), ("fAutoLogonIfChallenged", wintypes.BOOL)]

    class Bilgi(ctypes.Structure):
        _fields_ = [("dwAccessType", wintypes.DWORD), ("lpszProxy", ctypes.c_void_p),
                    ("lpszProxyBypass", ctypes.c_void_p)]

    if pac_adresi is None:
        pac_adresi = (ayarlar() or {}).get("pac_adresi")
    try:
        winhttp = ctypes.WinDLL("winhttp.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll")
        winhttp.WinHttpOpen.restype = ctypes.c_void_p
        winhttp.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD]
        winhttp.WinHttpGetProxyForUrl.restype = wintypes.BOOL
        winhttp.WinHttpGetProxyForUrl.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(Secenek),
                                                  ctypes.POINTER(Bilgi)]
        winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        handle = winhttp.WinHttpOpen("service-test-mcp", 1, None, None, 0)  # 1 = no proxy for the PAC download
        if not handle:
            return {"hata": f"WinHttpOpen başarısız ({ctypes.get_last_error()})"}
        try:
            secenek = Secenek()
            if pac_adresi:
                secenek.dwFlags = 0x2                     # WINHTTP_AUTOPROXY_CONFIG_URL
                secenek.lpszAutoConfigUrl = pac_adresi
            else:
                secenek.dwFlags = 0x1                     # WINHTTP_AUTOPROXY_AUTO_DETECT
                secenek.dwAutoDetectFlags = 0x1 | 0x2     # DHCP | DNS
            secenek.fAutoLogonIfChallenged = True
            bilgi = Bilgi()
            if not winhttp.WinHttpGetProxyForUrl(handle, url, ctypes.byref(secenek), ctypes.byref(bilgi)):
                code = ctypes.get_last_error()
                return {"kaynak": f"PAC: {pac_adresi}" if pac_adresi else "otomatik algılama (WPAD)",
                        "hata": f"{HATALAR.get(code, 'WinHTTP hatası')} ({code})"}
            ham = ctypes.wstring_at(bilgi.lpszProxy) if bilgi.lpszProxy else ""
            for ptr in (bilgi.lpszProxy, bilgi.lpszProxyBypass):
                if ptr:
                    kernel32.GlobalFree(ptr)
        finally:
            winhttp.WinHttpCloseHandle(handle)
    except Exception as exc:  # noqa: BLE001 - shown to the user, never fatal
        return {"hata": f"{type(exc).__name__}: {exc}"}
    proxyler = proxy_adresleri(ham) if bilgi.dwAccessType == 3 else []
    return {"kaynak": f"PAC: {pac_adresi}" if pac_adresi else "otomatik algılama (WPAD)", "ham": ham or "DIRECT",
            "yol": "proxy" if proxyler else "doğrudan", "proxyler": proxyler}
