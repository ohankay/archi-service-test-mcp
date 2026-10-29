"""HTTP sessions: one ``requests.Session`` per (service, environment), shared by SOAP (zeep) and REST.

Every request - including WSDL/XSD downloads and redirects - passes the host allowlist check.
"""

from __future__ import annotations

import sys
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .config import ConfigError, EnvironmentConfig, ServiceConfig, Settings, secret_from_env


class GuardedSession(requests.Session):
    def __init__(self, settings: Settings, service: ServiceConfig, env: EnvironmentConfig) -> None:
        super().__init__()
        self._stm = (settings, service, env)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        settings, service, env = self._stm
        settings.check_host(request.url or "", service, env)
        return super().send(request, **kwargs)


class _HeaderAuth(requests.auth.AuthBase):
    def __init__(self, name: str, value: str) -> None:
        self.name, self.value = name, value

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers[self.name] = self.value
        return r


def build_session(settings: Settings, service: ServiceConfig, env: EnvironmentConfig) -> tuple[GuardedSession, list[str]]:
    """Returns the session and the secret values it uses (so they can be redacted from any output)."""
    session = GuardedSession(settings, service, env)
    secrets: list[str] = []
    session.verify = env.verify_tls
    if env.proxy:
        choice = str(env.proxy).strip().lower()
        if choice in ("none", "off", "direct"):
            session.trust_env = False
        elif choice in ("auto", "otomatik"):
            # like the browser: Windows evaluates the PAC script for this address -> a proxy or direct
            from . import winproxy

            base = service.value_for("base_url", env.name) or ""
            decision = winproxy.decision(base) if base else None
            session.stm_proxy_decision = decision  # type: ignore[attr-defined]  # shown by check_connection
            if decision and decision.get("proxies"):
                session.proxies = {"http": decision["proxies"][0], "https": decision["proxies"][0]}
            elif decision and decision.get("route") == "direct":
                session.trust_env = False
        else:
            session.proxies = {"http": env.proxy, "https": env.proxy}
    session.headers.update({"User-Agent": "service-test-mcp"})
    session.headers.update(env.headers)
    session.headers.update(service.headers)

    auth = service.auth_for(env.name)
    where = f"services.{service.name}.auth"
    if auth.type == "basic":
        pw = secret_from_env(auth.password_env or "", where)
        secrets.append(pw)
        session.auth = HTTPBasicAuth(auth.user or "", pw)
    elif auth.type == "ntlm":
        from requests_ntlm import HttpNtlmAuth

        pw = secret_from_env(auth.password_env or "", where)
        secrets.append(pw)
        session.auth = HttpNtlmAuth(auth.user or "", pw)
    elif auth.type == "windows":
        if sys.platform != "win32":
            raise ConfigError(f"{where}: 'windows' authentication (logged-on user, Kerberos/NTLM via SSPI) works only "
                              "on Windows. Use 'ntlm' with user/password_env elsewhere.")
        from requests_negotiate_sspi import HttpNegotiateAuth

        session.auth = HttpNegotiateAuth()
    elif auth.type == "bearer":
        token = secret_from_env(auth.token_env or "", where)
        secrets.append(token)
        session.auth = _HeaderAuth("Authorization", f"Bearer {token}")
    elif auth.type == "api_key":
        value = secret_from_env(auth.value_env or "", where)
        secrets.append(value)
        session.auth = _HeaderAuth(auth.header, value)
    elif auth.type == "client_cert":
        session.cert = (auth.cert_file, auth.key_file) if auth.key_file else auth.cert_file
    elif auth.type == "ws_username_token":
        if service.protocol != "soap":
            raise ConfigError(f"{where}: 'ws_username_token' is only for SOAP services")
        secrets.append(secret_from_env(auth.password_env or "", where))
    return session, secrets
