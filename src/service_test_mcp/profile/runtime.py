"""Calls through a profile: the agent names an operation (and maybe a persona); everything else happens here.

For a call this module

1. finds the operation in the catalog and applies the mutating guard of the environment,
2. picks the persona (test customer) and takes its lease, so nobody else logs in with it at the same time,
3. logs in with the profile's login steps when there is no live session (or it is about to run out),
4. builds the request: URL from the catalog path, body = envelope + params, header templates,
5. sends it through the engine (host allowlist, masking and audit log apply as for any REST call),
6. evaluates the business success rule, and when the server says the session is gone, logs in again and
   repeats the call once - for mutating operations only if the profile allows it, because a repeated money
   transfer is worse than a failed one.

Sessions live in this process, one per (environment, persona). Calls with the same persona are serialized.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import ConfigError, EnvironmentConfig
from ..scenario.asserts import evaluate, find
from ..scenario.template import Context
from .leases import LeaseStore
from .model import Operation, PersonaSpec, Profile, StepSpec

if TYPE_CHECKING:
    from ..engine import Engine

NO_PERSONA = "_default"


class ProfileError(ConfigError):
    """An expected failure of a profile call (unknown operation, busy persona, failed login ...)."""


class PersonaBusy(ProfileError):
    pass


class LoginFailed(ProfileError):
    pass


@dataclass
class SessionState:
    environment: str
    persona: str
    lease_key: str
    persona_vars: dict[str, Any]
    vars: dict[str, Any] = field(default_factory=dict)
    created: float = 0.0
    last_used: float = 0.0
    logins: int = 0
    logged_in: bool = False

    def alive(self, profile: Profile, now: float | None = None) -> bool:
        if not self.logged_in:
            return False
        if not profile.session.login:
            return True  # nothing to expire
        now = now or time.time()
        base = self.created if profile.session.ttl_mode == "absolute" else self.last_used
        return now - base < profile.session.ttl_s - profile.session.refresh_margin_s

    def public(self, profile: Profile) -> dict[str, Any]:
        now = time.time()
        base = self.created if profile.session.ttl_mode == "absolute" else self.last_used
        return {"environment": self.environment, "persona": self.persona, "logged_in": self.logged_in,
                "logins": self.logins, "session_age_s": round(now - self.created) if self.created else None,
                "expires_in_s": max(0, round(profile.session.ttl_s - (now - base))) if self.logged_in else None}


def _load_hooks(profile: Profile) -> Any:
    if not profile.hooks:
        return None
    target, _, attr = profile.hooks.partition(":")
    if not attr:
        raise ConfigError(f"profile hooks must look like 'file.py:ClassName' or 'package.module:ClassName', "
                          f"got {profile.hooks!r}")
    if target.endswith(".py"):
        file = Path(target)
        if not file.is_absolute():
            file = profile.path.parent / file
        spec = importlib.util.spec_from_file_location(f"stm_profile_hooks_{abs(hash(str(file)))}", file)
        if spec is None or spec.loader is None:
            raise ConfigError(f"cannot load hooks file {file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    cls = getattr(module, attr, None)
    if cls is None:
        raise ConfigError(f"hooks: {attr} not found in {target}")
    return cls(profile) if isinstance(cls, type) else cls


class ProfileRuntime:
    def __init__(self, engine: "Engine", service: str, profile: Profile, leases: LeaseStore, owner: str,
                 wait_s: float = 0.0) -> None:
        self.engine = engine
        self.service = service
        self.profile = profile
        self.leases = leases
        self.owner = owner
        self.wait_s = wait_s
        self.sessions: dict[tuple[str, str], SessionState] = {}
        self._held: dict[str, tuple[str, str]] = {}          # lease key -> (environment, persona) in this process
        self._locks: dict[tuple[str, str], threading.RLock] = {}
        self._guard = threading.RLock()
        self.hooks = _load_hooks(profile)

    # -- lookup -------------------------------------------------------------------------------------------------
    def operation(self, name: str) -> Operation:
        op = self.profile.catalog.get(name)
        if op is None:
            known = sorted(self.profile.catalog.operations)
            near = [k for k in known if name.lower() in k.lower()][:10]
            raise ProfileError(f"Operation '{name}' is not in the catalog of '{self.service}'"
                               + (f" (similar: {', '.join(near)})" if near else "")
                               + ". Use list_operations, or add it with import_traffic_log from a log that contains "
                                 "it.")
        return op

    def _key_lock(self, key: tuple[str, str]) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())

    def _persona_name(self, env: EnvironmentConfig, persona: str | None) -> str:
        personas = self.profile.environment(env.name).personas
        if persona:
            if persona not in personas:
                raise ProfileError(f"Unknown persona '{persona}' for '{self.service}' in environment '{env.name}'. "
                                   f"Defined: {', '.join(personas) or '(none)'}")
            return persona
        if self.profile.default_persona and self.profile.default_persona in personas:
            return self.profile.default_persona
        if len(personas) == 1:
            return next(iter(personas))
        if not personas:
            return NO_PERSONA
        raise ProfileError(f"'{self.service}' needs a persona in environment '{env.name}': "
                           f"{', '.join(personas)} (or set personas.default in the profile)")

    def _context(self, env: EnvironmentConfig, session_vars: dict[str, Any], persona_vars: dict[str, Any],
                 operation: str) -> Context:
        env_vars = {**self.profile.environment(env.name).variables, **env.variables}
        return Context(variables={"session": session_vars, "persona": persona_vars, "operation": operation},
                       env_variables=env_vars, on_secret=self.engine.masker.add_secret)

    # -- one HTTP exchange ----------------------------------------------------------------------------------------
    def build_request(self, env: EnvironmentConfig, op: Operation, params: dict[str, Any] | None,
                      session_vars: dict[str, Any], persona_vars: dict[str, Any]) -> dict[str, Any]:
        ctx = self._context(env, session_vars, persona_vars, op.name)
        body = ctx.render(self.profile.envelope)
        if persona_vars:
            body.update(ctx.render(self.profile.session_envelope))
        body.update(ctx.render(params or {}))
        headers = {str(k): str(v) for k, v in (ctx.render(self.profile.headers) or {}).items()
                   if v is not None and v != ""}
        path = self.profile.url.replace("{path}", op.path).replace("{operation}", op.name)
        while "//" in path:
            path = path.replace("//", "/")
        request = {"method": op.method or self.profile.method, "path": "/" + path.strip("/"), "headers": headers,
                   "body": body}
        if self.hooks is not None and hasattr(self.hooks, "prepare"):
            self.hooks.prepare(request, {"environment": env.name, "operation": op.name, "session": session_vars,
                                         "persona": persona_vars})
        return request

    def _send(self, env: EnvironmentConfig, op: Operation, params: dict[str, Any] | None,
              session_vars: dict[str, Any], persona_vars: dict[str, Any], origin: str,
              persona: str | None) -> dict[str, Any]:
        req = self.build_request(env, op, params, session_vars, persona_vars)
        result = self.engine.rest_call(self.service, req["method"], req["path"], None, req["headers"] or None,
                                       req["body"], None, None, env.name, origin=origin, label=op.name)
        result["operation"] = op.name
        if persona and persona != NO_PERSONA:
            result["persona"] = persona
        result["business"] = self.business(result)
        if self.hooks is not None and hasattr(self.hooks, "inspect"):
            self.hooks.inspect(result, {"environment": env.name, "operation": op.name, "session": session_vars,
                                        "persona": persona_vars})
        return result

    def business(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("error"):
            return {"success": False, "errors": [result["error"].get("message")]}
        if self.profile.success:
            ok = all(c["passed"] for c in evaluate(self.profile.success, result))
        else:
            ok = int(result.get("status") or 0) < 400
        out: dict[str, Any] = {"success": ok}
        if not ok:
            errors: list[Any] = []
            for path in self.profile.error_paths:
                try:
                    errors += [v for v in find(result.get("body"), path) if v not in (None, "")]
                except Exception:  # noqa: BLE001 - a body that is not JSON
                    pass
            if not errors and result.get("status") and int(result["status"]) >= 400:
                errors.append(f"HTTP {result['status']}")
            out["errors"] = errors[:10]
        return out

    def _expired(self, result: dict[str, Any]) -> bool:
        rules = self.profile.session.expired_when
        return bool(rules) and all(c["passed"] for c in evaluate(rules, result))

    # -- personas and leases ---------------------------------------------------------------------------------------
    def _lease_key(self, env: str, persona_vars: dict[str, Any], persona: str) -> str:
        fields = self.profile.identity
        if persona == NO_PERSONA:
            ident = NO_PERSONA
        elif fields and all(persona_vars.get(f) not in (None, "") for f in fields):
            ident = "|".join(f"{f}={persona_vars[f]}" for f in fields)
        else:
            scalars = {k: v for k, v in sorted(persona_vars.items()) if isinstance(v, (str, int, float, bool))}
            ident = json.dumps(scalars, ensure_ascii=False, sort_keys=True)
        return f"{self.service}|{env}|{ident}"

    def _try_lease(self, env: str, persona: str, persona_vars: dict[str, Any]) -> str | None:
        key = self._lease_key(env, persona_vars, persona)
        with self._guard:
            other = self._held.get(key)
            if other and other != (env, persona):
                return None  # the same customer under another persona name in this process: no parallel login
            info = {"persona": persona, "service": self.service, "environment": env}
            if not self.leases.acquire(key, self.owner, self.profile.session.ttl_s, info):
                return None
            self._held[key] = (env, persona)
            return key

    def _release(self, key: str) -> None:
        with self._guard:
            self._held.pop(key, None)
        try:
            self.leases.release(key, self.owner)
        except Exception:  # noqa: BLE001 - never let cleanup hide the real error
            pass

    def _from_source(self, env: EnvironmentConfig, persona: PersonaSpec) -> dict[str, Any]:
        step = persona.source
        assert step is not None
        op = self.operation(step.operation)
        if op.session:
            raise ProfileError(f"persona '{persona.name}': source operation '{op.name}' must not need a session "
                               "(set session: false in the catalog)")
        result = self._send(env, op, step.params, dict(self.profile.session.defaults), {}, "persona", None)
        if not result["business"]["success"]:
            raise ProfileError(f"persona '{persona.name}': {op.name} failed: {result['business'].get('errors')}")
        out: dict[str, Any] = {}
        for name, path in step.extract.items():
            values = find(result.get("body"), path)
            if not values:
                raise ProfileError(f"persona '{persona.name}': {path} not found in the {op.name} response")
            out[name] = values[0]
        return out

    def _acquire_persona(self, env: EnvironmentConfig, name: str) -> tuple[dict[str, Any], str]:
        if name == NO_PERSONA:
            key = self._try_lease(env.name, name, {})
            if key:
                return {}, key
            return self._busy(env, name, [self._lease_key(env.name, {}, name)])
        spec = self.profile.environment(env.name).personas[name]
        deadline = time.monotonic() + self.wait_s
        while True:
            tried: list[str] = []
            if spec.source:
                for _ in range(3):
                    values = self._from_source(env, spec)
                    key = self._try_lease(env.name, name, values)
                    if key:
                        return values, key
                    tried.append(self._lease_key(env.name, values, name))
            else:
                ctx = self._context(env, {}, {}, "")
                for candidate in spec.pool:
                    values = ctx.render(candidate)
                    key = self._try_lease(env.name, name, values)
                    if key:
                        return values, key
                    tried.append(self._lease_key(env.name, values, name))
            if time.monotonic() >= deadline:
                return self._busy(env, name, tried)
            time.sleep(0.5)

    def _busy(self, env: EnvironmentConfig, name: str, keys: list[str]) -> Any:
        holders = []
        for key in keys:
            holder = self.leases.holder(key)
            local = self._held.get(key)
            if local:
                holders.append({"owner": "this process", "persona": local[1]})
            elif holder:
                holders.append(holder)
        raise PersonaBusy(f"Persona '{name}' ({env.name}) is in use - parallel logins with the same test customer "
                          f"are not allowed. Holders: {json.dumps(holders, ensure_ascii=False)}. Wait for the lease "
                          "to end, add customers to the persona's pool, or use another persona.")

    # -- sessions -------------------------------------------------------------------------------------------------
    def _login(self, env: EnvironmentConfig, st: SessionState) -> None:
        st.vars = dict(self.profile.session.defaults)
        st.logged_in = False
        for step in self.profile.session.login:
            op = self.profile.catalog.get(step.operation)
            if op is None:
                raise LoginFailed(f"login step '{step.operation}' is not in the catalog of '{self.service}'")
            result = self._send(env, op, step.params, st.vars, st.persona_vars, "login", st.persona)
            if not result["business"]["success"]:
                raise LoginFailed(f"login step {op.name} failed for persona '{st.persona}' ({env.name}): "
                                  f"{result['business'].get('errors') or result.get('status')}")
            for name, path in step.extract.items():
                values = find(result.get("body"), path)
                if not values:
                    raise LoginFailed(f"login step {op.name}: {path} not found in the response")
                st.vars[name] = values[0]
                if isinstance(values[0], str):
                    self.engine.masker.add_secret(values[0])
        st.created = st.last_used = time.time()
        st.logins += 1
        st.logged_in = True

    def _end(self, st: SessionState, logout: bool) -> dict[str, Any]:
        out = {"environment": st.environment, "persona": st.persona, "logged_out": False}
        if logout and st.logged_in and self.profile.session.logout and st.alive(self.profile):
            try:
                env = self.engine.settings.environment(st.environment)
                step: StepSpec = self.profile.session.logout
                op = self.profile.catalog.get(step.operation)
                if op is not None:
                    res = self._send(env, op, step.params, st.vars, st.persona_vars, "logout", st.persona)
                    out["logged_out"] = bool(res["business"]["success"])
            except Exception as exc:  # noqa: BLE001
                out["logout_error"] = str(exc)[:300]
        self.sessions.pop((st.environment, st.persona), None)
        self._release(st.lease_key)
        return out

    def session(self, env: EnvironmentConfig, persona: str | None, fresh: bool = False) -> SessionState:
        name = self._persona_name(env, persona)
        key = (env.name, name)
        with self._key_lock(key):
            st = self.sessions.get(key)
            if st and fresh:
                self._end(st, logout=True)
                st = None
            if st is not None:
                if not self.leases.renew(st.lease_key, self.owner, self.profile.session.ttl_s):
                    # the lease ran out (long pause): someone else may hold this customer now
                    self.sessions.pop(key, None)
                    with self._guard:
                        self._held.pop(st.lease_key, None)
                    st = None
                elif not st.alive(self.profile):
                    self._login(env, st)
                    return st
                else:
                    return st
            persona_vars, lease_key = self._acquire_persona(env, name)
            st = SessionState(environment=env.name, persona=name, lease_key=lease_key, persona_vars=persona_vars)
            try:
                self._login(env, st)
            except Exception:
                self._release(lease_key)
                raise
            self.sessions[key] = st
            return st

    # -- public API -----------------------------------------------------------------------------------------------
    def call(self, operation: str, params: dict[str, Any] | None = None, persona: str | None = None,
             environment: str | None = None, origin: str = "tool", fresh_session: bool = False) -> dict[str, Any]:
        svc, env = self.engine.resolve(self.service, environment)
        op = self.operation(operation)
        if op.mutating and not env.allow_mutating:
            raise ProfileError(f"'{op.name}' of '{svc.name}' is {op.kind}"
                               + (" (not classified: set kind in the catalog)" if op.kind == "unknown" else "")
                               + f"; environment '{env.name}' has allow_mutating: false.")
        if not op.session:
            name = self._persona_name(env, persona) if (persona or self.profile.environment(env.name).personas) \
                else NO_PERSONA
            live = self.sessions.get((env.name, name))
            session_vars = live.vars if live and live.alive(self.profile) else dict(self.profile.session.defaults)
            persona_vars = live.persona_vars if live else {}
            return self._send(env, op, params, session_vars, persona_vars, origin, name if live else None)
        name = self._persona_name(env, persona)
        with self._key_lock((env.name, name)):
            st = self.session(env, persona, fresh_session)
            result = self._send(env, op, params, st.vars, st.persona_vars, origin, st.persona)
            if self._expired(result):
                st.logged_in = False
                if not op.mutating or self.profile.session.retry_mutating_on_expired:
                    st = self.session(env, persona)
                    result = self._send(env, op, params, st.vars, st.persona_vars, origin, st.persona)
                    result["session_renewed"] = True
                else:
                    result["session_expired"] = ("the server ended the session; this mutating call was not repeated "
                                                 "automatically (retry_mutating_on_expired: false). Call it again "
                                                 "to log in and retry.")
            st.last_used = time.time()
            return result

    def persona_values(self, environment: str, persona: str | None) -> dict[str, Any]:
        """Fields of the persona logged in for (environment, persona) - for {{persona.x}} in assertions."""
        env = self.engine.settings.environment(environment)
        st = self.sessions.get((env.name, self._persona_name(env, persona)))
        return dict(st.persona_vars) if st else {}

    def end_sessions(self, environment: str | None = None, persona: str | None = None,
                     logout: bool = True) -> list[dict[str, Any]]:
        ended = []
        for (env, name), st in list(self.sessions.items()):
            if environment and env != environment:
                continue
            if persona and name != persona:
                continue
            with self._key_lock((env, name)):
                ended.append(self._end(st, logout))
        return ended

    def status(self, environment: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"service": self.service, **self.profile.public(environment)}
        out["sessions"] = [st.public(self.profile) for st in self.sessions.values()
                           if not environment or st.environment == environment]
        if environment:
            busy = {}
            for p in self.profile.environment(environment).personas.values():
                if p.source:
                    continue
                for candidate in p.pool:
                    try:
                        values = self._context(self.engine.settings.environment(environment), {}, {}, "") \
                            .render(candidate)
                    except Exception:  # noqa: BLE001 - e.g. a secret that is not set
                        continue
                    holder = self.leases.holder(self._lease_key(environment, values, p.name))
                    if holder and holder.get("owner") != self.owner:
                        busy.setdefault(p.name, []).append(holder)
            if busy:
                out["busy_elsewhere"] = busy
        return out

    def close(self) -> None:
        self.end_sessions(logout=True)
