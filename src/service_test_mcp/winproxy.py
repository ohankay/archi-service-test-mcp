"""What Windows (and so the browser) would use to reach a URL: a proxy or a direct connection.

Python's requests reads only HTTPS_PROXY and the fixed proxy in the Windows settings; it does not run the PAC
(automatic configuration) script that browsers use. Here Windows itself evaluates it (WinHTTP), so the answer is the
same as the browser's: "DIRECT" or "PROXY host:port".
"""

from __future__ import annotations

import re
import sys
from typing import Any

ERRORS = {12180: "automatic detection (WPAD) found no proxy script",
          12167: "the PAC file could not be downloaded",
          12166: "the PAC file failed while running",
          12178: "the Windows auto-proxy service failed",
          12005: "invalid address"}


def settings() -> dict[str, Any] | None:
    """Proxy settings of the current Windows user (Internet Options). None outside Windows."""
    if sys.platform != "win32":
        return None
    import winreg

    out: dict[str, Any] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            for name, label in (("ProxyEnable", "fixed_proxy_enabled"), ("ProxyServer", "fixed_proxy"),
                                ("ProxyOverride", "proxy_bypass"), ("AutoConfigURL", "pac_url")):
                try:
                    out[label] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
    except OSError as exc:
        return {"error": str(exc)}
    if "fixed_proxy_enabled" in out:
        out["fixed_proxy_enabled"] = bool(out["fixed_proxy_enabled"])
    return out


def proxy_urls(text: str | None) -> list[str]:
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


def decision(url: str, pac_url: str | None = None) -> dict[str, Any] | None:
    """Ask Windows (WinHTTP) which way it would reach url. {'route': 'direct'|'proxy', 'proxies': [...]}"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Options(ctypes.Structure):
        _fields_ = [("dwFlags", wintypes.DWORD), ("dwAutoDetectFlags", wintypes.DWORD),
                    ("lpszAutoConfigUrl", wintypes.LPCWSTR), ("lpvReserved", ctypes.c_void_p),
                    ("dwReserved", wintypes.DWORD), ("fAutoLogonIfChallenged", wintypes.BOOL)]

    class Info(ctypes.Structure):
        _fields_ = [("dwAccessType", wintypes.DWORD), ("lpszProxy", ctypes.c_void_p),
                    ("lpszProxyBypass", ctypes.c_void_p)]

    if pac_url is None:
        pac_url = (settings() or {}).get("pac_url")
    source = f"PAC: {pac_url}" if pac_url else "automatic detection (WPAD)"
    try:
        winhttp = ctypes.WinDLL("winhttp.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll")
        winhttp.WinHttpOpen.restype = ctypes.c_void_p
        winhttp.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD]
        winhttp.WinHttpGetProxyForUrl.restype = wintypes.BOOL
        winhttp.WinHttpGetProxyForUrl.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(Options),
                                                  ctypes.POINTER(Info)]
        winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        handle = winhttp.WinHttpOpen("service-test-mcp", 1, None, None, 0)  # 1 = no proxy for the PAC download
        if not handle:
            return {"error": f"WinHttpOpen failed ({ctypes.get_last_error()})"}
        try:
            options = Options()
            if pac_url:
                options.dwFlags = 0x2                     # WINHTTP_AUTOPROXY_CONFIG_URL
                options.lpszAutoConfigUrl = pac_url
            else:
                options.dwFlags = 0x1                     # WINHTTP_AUTOPROXY_AUTO_DETECT
                options.dwAutoDetectFlags = 0x1 | 0x2     # DHCP | DNS
            options.fAutoLogonIfChallenged = True
            info = Info()
            if not winhttp.WinHttpGetProxyForUrl(handle, url, ctypes.byref(options), ctypes.byref(info)):
                code = ctypes.get_last_error()
                return {"source": source, "error": f"{ERRORS.get(code, 'WinHTTP error')} ({code})"}
            raw = ctypes.wstring_at(info.lpszProxy) if info.lpszProxy else ""
            for ptr in (info.lpszProxy, info.lpszProxyBypass):
                if ptr:
                    kernel32.GlobalFree(ptr)
        finally:
            winhttp.WinHttpCloseHandle(handle)
    except Exception as exc:  # noqa: BLE001 - shown to the user, never fatal
        return {"error": f"{type(exc).__name__}: {exc}"}
    proxies = proxy_urls(raw) if info.dwAccessType == 3 else []
    return {"source": source, "raw": raw or "DIRECT", "route": "proxy" if proxies else "direct", "proxies": proxies}
