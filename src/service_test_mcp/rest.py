"""REST adapter (requests) with optional OpenAPI / Swagger discovery."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import ConfigError, EnvironmentConfig, ServiceConfig, Settings
from .iz import siniflandir
from .normalize import parse_json_text
from .transport import GuardedSession, build_session

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
MAX_TEXT_BODY = 200_000


class RestAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[tuple[str, str], tuple[GuardedSession, list[str]]] = {}
        self._openapi: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        for session, _ in self._sessions.values():
            session.close()
        self._sessions.clear()
        self._openapi.clear()

    def session(self, service: ServiceConfig, env: EnvironmentConfig) -> tuple[GuardedSession, list[str]]:
        key = (service.name, env.name)
        with self._lock:
            if key not in self._sessions:
                self._sessions[key] = build_session(self.settings, service, env)
            return self._sessions[key]

    def url_for(self, service: ServiceConfig, env: EnvironmentConfig, path: str) -> str:
        base = service.value_for("base_url", env.name)
        if not base:
            raise ConfigError(f"Service '{service.name}' has no 'base_url' for environment '{env.name}'")
        path = path or ""
        if "://" in path:
            if not path.startswith(base.rstrip("/")):
                raise ConfigError(f"Absolute URLs are only allowed under the service base_url ({base}); give a "
                                  "path relative to it instead")
            return path
        if ".." in path.split("?")[0].split("/"):
            raise ConfigError("'..' is not allowed in paths")
        return base.rstrip("/") + "/" + path.lstrip("/") if path else base

    def call(self, service: ServiceConfig, env: EnvironmentConfig, method: str, path: str,
             query: dict[str, Any] | None = None, headers: dict[str, Any] | None = None, body: Any = None,
             body_text: str | None = None, form: dict[str, Any] | None = None) -> dict[str, Any]:
        method = (method or "GET").upper()
        if method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        route = "/" + (urlsplit(path).path if "://" in (path or "") else (path or "").split("?")[0]).lstrip("/")
        if service.is_mutating(route, method) and not env.allow_mutating:
            raise ConfigError(f"'{method} {route}' of '{service.name}' is marked as mutating; environment "
                              f"'{env.name}' has allow_mutating: false.")
        url = self.url_for(service, env, path)
        session, _ = self.session(service, env)
        timeout = service.timeout_s or env.timeout_s
        send_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        data: Any = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            send_headers = {"Content-Type": "application/json; charset=utf-8", **send_headers}
        elif body_text is not None:
            data = body_text.encode("utf-8")
        elif form is not None:
            data = {str(k): str(v) for k, v in form.items()}
        result: dict[str, Any] = {"protocol": "rest", "service": service.name, "environment": env.name,
                                  "method": method, "path": route, "status": None, "fault": None, "body": None,
                                  "headers": {}}
        # prepared by hand so the trace shows what really goes out: the session's headers, auth, proxy, TLS setting
        prepared = session.prepare_request(requests.Request(method, url, params=query or None,
                                                            headers=send_headers or None, data=data))
        settings = session.merge_environment_settings(prepared.url, {}, None, session.verify, None)
        proxies = settings.get("proxies") or {}
        proxy = proxies.get(urlsplit(prepared.url or url).scheme) or proxies.get("all")
        result["istek"] = {"metod": method, "url": prepared.url, "basliklar": dict(prepared.headers),
                           "govde": body if body is not None else (body_text if body_text is not None else form),
                           "zaman_asimi_sn": timeout, "proxy": proxy or None, "tls_dogrulama": settings.get("verify")}
        started = time.perf_counter()
        try:
            resp = session.send(prepared, timeout=timeout, allow_redirects=True, **settings)
        except ConfigError as exc:
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            exc.istek = result["istek"]  # type: ignore[attr-defined]  # traced by the engine, then re-raised
            raise
        except Exception as exc:  # noqa: BLE001 - connection errors are results, not crashes
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            detail = siniflandir(exc, prepared.url or url, timeout, proxy)
            result["error"] = {"type": type(exc).__name__, "message": detail["asama"] + " - " + str(exc)[:1500],
                               **detail}
            return result
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["status"] = resp.status_code
        result["reason"] = resp.reason
        result["url"] = resp.url
        result["headers"] = dict(resp.headers)
        if resp.history:
            result["redirects"] = [f"{r.status_code} {r.headers.get('Location', '')}" for r in resp.history]
        ctype = resp.headers.get("Content-Type", "")
        text = resp.content.decode(resp.encoding or "utf-8", "replace") if resp.content else ""
        if text and ("json" in ctype or text.lstrip()[:1] in ("{", "[")):
            try:
                result["body"] = parse_json_text(text)
            except ValueError:
                result["body"] = text[:MAX_TEXT_BODY]
        else:
            result["body"] = text[:MAX_TEXT_BODY] if text else None
        return result

    # -- OpenAPI -------------------------------------------------------------------------------------------------
    def openapi(self, service: ServiceConfig, env: EnvironmentConfig) -> dict[str, Any] | None:
        location = service.value_for("openapi", env.name)
        if not location:
            return None
        key = (service.name, env.name)
        if key in self._openapi:
            return self._openapi[key]
        if "://" in location:
            session, _ = self.session(service, env)
            resp = session.get(location, timeout=service.timeout_s or env.timeout_s)
            resp.raise_for_status()
            text = resp.text
        else:
            path = os.path.expanduser(location)
            if not os.path.isabs(path) and self.settings.path:
                path = os.path.join(os.path.dirname(self.settings.path), path)
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
        try:
            doc = json.loads(text)
        except ValueError:
            import yaml

            doc = yaml.safe_load(text)
        if not isinstance(doc, dict):
            raise ConfigError(f"OpenAPI document of '{service.name}' is not a mapping")
        self._openapi[key] = doc
        return doc

    def describe_service(self, service: ServiceConfig, env: EnvironmentConfig) -> dict[str, Any]:
        out: dict[str, Any] = {"base_url": service.value_for("base_url", env.name)}
        doc = self.openapi(service, env)
        if doc is None:
            out["operations"] = None
            out["note"] = "No 'openapi' configured: call rest_call with method + path directly."
            return out
        ops = []
        for route, item in (doc.get("paths") or {}).items():
            for method, op in (item or {}).items():
                if method.upper() in METHODS and isinstance(op, dict):
                    ops.append({"operation": f"{method.upper()} {route}", "operationId": op.get("operationId"),
                                "summary": op.get("summary"),
                                "mutating": service.is_mutating(route, method)})
        out["title"] = (doc.get("info") or {}).get("title")
        out["operations"] = ops
        return out

    def describe_operation(self, service: ServiceConfig, env: EnvironmentConfig, operation: str) -> dict[str, Any]:
        doc = self.openapi(service, env)
        if doc is None:
            raise ConfigError(f"Service '{service.name}' has no 'openapi' configured; describe_operation needs it. "
                              "Call rest_call with method + path directly.")
        wanted = operation.strip()
        for route, item in (doc.get("paths") or {}).items():
            for method, op in (item or {}).items():
                if method.upper() not in METHODS or not isinstance(op, dict):
                    continue
                if wanted in (op.get("operationId"), f"{method.upper()} {route}") or \
                        wanted.lower() == f"{method} {route}".lower():
                    params = list(item.get("parameters") or []) + list(op.get("parameters") or [])
                    return {"operation": f"{method.upper()} {route}", "operationId": op.get("operationId"),
                            "summary": op.get("summary"), "description": op.get("description"),
                            "parameters": _resolve(doc, params),
                            "request_body": _resolve(doc, op.get("requestBody")),
                            "responses": _resolve(doc, op.get("responses")),
                            "mutating": service.is_mutating(route, method)}
        raise ConfigError(f"Operation '{operation}' not found in the OpenAPI document; use 'METHOD /path' or an "
                          "operationId (see describe_service)")


def _resolve(doc: dict[str, Any], node: Any, depth: int = 0, seen: frozenset = frozenset()) -> Any:
    if depth > 12:
        return {"note": "too deep"}
    if isinstance(node, list):
        return [_resolve(doc, x, depth + 1, seen) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {"$ref": ref, "note": "recursive"}
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        return _resolve(doc, target, depth + 1, seen | {ref})
    return {k: _resolve(doc, v, depth + 1, seen) for k, v in node.items()}
