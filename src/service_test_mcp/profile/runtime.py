"""Calls through a profile: the agent names an operation (and maybe a test_customer); everything else happens here.

For a call this module

1. finds the operation in the catalog and applies the mutating guard of the environment,
2. picks the test customer and takes its lease, so nobody else logs in with it at the same time,
3. logs in with the profile's login steps when there is no live session (or it is about to run out),
4. builds the request: URL from the catalog path, body = envelope + params, header templates,
5. sends it through the engine (host allowlist, masking and audit log apply as for any REST call),
6. evaluates the business success rule, and when the server says the session is gone, logs in again and
   repeats the call once - for mutating operations only if the profile allows it, because a repeated money
   transfer is worse than a failed one.

Sessions live in this process, one per (environment, test_customer). Calls with the same test_customer are serialized.
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
from .model import Operation, CustomerSpec, Profile, StepSpec

if TYPE_CHECKING:
    from ..engine import Engine

NO_TEST_CUSTOMER = "_default"


class ProfileError(ConfigError):
    """An expected failure of a profile call (unknown operation, busy test_customer, failed login ...)."""


class CustomerBusy(ProfileError):
    pass


class LoginFailed(ProfileError):
    pass


@dataclass
class SessionState:
    environment: str
    test_customer: str
    lease_key: str
    test_customer_vars: dict[str, Any]
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
        return {"environment": self.environment, "test_customer": self.test_customer, "logged_in": self.logged_in,
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
        self._held: dict[str, tuple[str, str]] = {}          # lease key -> (environment, test_customer) in this process
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

    def _test_customer_name(self, env: EnvironmentConfig, test_customer: str | None) -> str:
        test_customers = self.profile.environment(env.name).test_customers
        if test_customer is not None and str(test_customer).strip() and self.profile.adhoc_field \
                and str(test_customer) not in test_customers:
            return str(test_customer).strip()          # a customer number given in the request
        default = self.profile.environment(env.name).default_customer
        if not test_customer and default:
            return str(default)
        if not test_customer and self.profile.adhoc_field and not test_customers:
            raise ProfileError(f"Hangi müşteriyle? '{self.service}' için müşteri numarası verin (customer=...) "
                               f"ya da '{env.name}' ortamına varsayilan_musteri yazın.")
        if test_customer:
            test_customer = str(test_customer)
            if test_customer not in test_customers:
                raise ProfileError(f"Unknown test_customer '{test_customer}' for '{self.service}' in environment "
                                   f"'{env.name}'. "
                                   f"Defined: {', '.join(test_customers) or '(none)'}")
            return test_customer
        if self.profile.default_test_customer and self.profile.default_test_customer in test_customers:
            return self.profile.default_test_customer
        if len(test_customers) == 1:
            return next(iter(test_customers))
        if not test_customers:
            return NO_TEST_CUSTOMER
        raise ProfileError(f"'{self.service}' needs a test_customer in environment '{env.name}': "
                           f"{', '.join(test_customers)} (or set test_customers.default in the profile)")

    def _context(self, env: EnvironmentConfig, session_vars: dict[str, Any], test_customer_vars: dict[str, Any],
                 operation: str) -> Context:
        env_vars = {**self.profile.environment(env.name).variables, **env.variables}
        return Context(variables={"session": session_vars, "test_customer": test_customer_vars, "operation": operation},
                       env_variables=env_vars, on_secret=self.engine.masker.add_secret,
                       secrets=self.engine.secrets(env.name))

    # -- one HTTP exchange ----------------------------------------------------------------------------------------
    def build_request(self, env: EnvironmentConfig, op: Operation, params: dict[str, Any] | None,
                      session_vars: dict[str, Any], test_customer_vars: dict[str, Any]) -> dict[str, Any]:
        ctx = self._context(env, session_vars, test_customer_vars, op.name)
        body = ctx.render(self.profile.envelope)
        if test_customer_vars:
            body.update(ctx.render(self.profile.session_envelope))
        body.update(ctx.render(params or {}))
        if self.profile.dotted_paths:          # definitions: a field set to null is not sent at all
            body = {k: v for k, v in body.items() if v is not None}
        headers = {str(k): str(v) for k, v in (ctx.render(self.profile.headers) or {}).items()
                   if v is not None and v != ""}
        path = self.profile.url.replace("{path}", op.path).replace("{operation}", op.name)
        while "//" in path:
            path = path.replace("//", "/")
        request = {"method": op.method or self.profile.method, "path": "/" + path.strip("/"), "headers": headers,
                   "body": body}
        if self.hooks is not None and hasattr(self.hooks, "prepare"):
            self.hooks.prepare(request, {"environment": env.name, "operation": op.name, "session": session_vars,
                                         "test_customer": test_customer_vars})
        return request

    def _send(self, env: EnvironmentConfig, op: Operation, params: dict[str, Any] | None,
              session_vars: dict[str, Any], test_customer_vars: dict[str, Any], origin: str,
              test_customer: str | None) -> dict[str, Any]:
        req = self.build_request(env, op, params, session_vars, test_customer_vars)
        result = self.engine.rest_call(self.service, req["method"], req["path"], None, req["headers"] or None,
                                       req["body"], None, None, env.name, origin=origin, label=op.name)
        result["operation"] = op.name
        if test_customer and test_customer != NO_TEST_CUSTOMER:
            result["test_customer"] = test_customer
        result["business"] = self.business(result)
        if self.hooks is not None and hasattr(self.hooks, "inspect"):
            self.hooks.inspect(result, {"environment": env.name, "operation": op.name, "session": session_vars,
                                        "test_customer": test_customer_vars})
        return result

    def business(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("error"):
            return {"success": False, "errors": [result["error"].get("message")]}
        if self.profile.success_lines:
            from ..tanim.kontrol import kontrol_et

            ok = all(c["gecti"] for c in kontrol_et(self.profile.success_lines, result, self.profile.error_paths))
        elif self.profile.success:
            ok = all(c["passed"] for c in evaluate(self.profile.success, result))
        else:
            ok = int(result.get("status") or 0) < 400
        out: dict[str, Any] = {"success": ok}
        if not ok:
            errors: list[Any] = []
            for path in self.profile.error_paths:
                try:
                    errors += [v for v in self._find(result.get("body"), path) if v not in (None, "")]
                except Exception:  # noqa: BLE001 - a body that is not JSON
                    pass
            if not errors and result.get("status") and int(result["status"]) >= 400:
                errors.append(f"HTTP {result['status']}")
            out["errors"] = errors[:10]
        return out

    def _expired(self, result: dict[str, Any]) -> bool:
        if self.profile.expired_lines:
            from ..tanim.kontrol import kontrol_et

            return all(c["gecti"] for c in kontrol_et(self.profile.expired_lines, result, self.profile.error_paths))
        rules = self.profile.session.expired_when
        return bool(rules) and all(c["passed"] for c in evaluate(rules, result))

    # -- test customers and leases ------------------------------------------------------------------------------------
    def _lease_key(self, env: str, test_customer_vars: dict[str, Any], test_customer: str) -> str:
        fields = self.profile.identity
        if test_customer == NO_TEST_CUSTOMER:
            ident = NO_TEST_CUSTOMER
        elif fields and all(test_customer_vars.get(f) not in (None, "") for f in fields):
            ident = "|".join(f"{f}={test_customer_vars[f]}" for f in fields)
        else:
            scalars = {k: v for k, v in sorted(test_customer_vars.items()) if isinstance(v, (str, int, float, bool))}
            ident = json.dumps(scalars, ensure_ascii=False, sort_keys=True)
        return f"{self.service}|{env}|{ident}"

    def _try_lease(self, env: str, test_customer: str, test_customer_vars: dict[str, Any]) -> str | None:
        key = self._lease_key(env, test_customer_vars, test_customer)
        with self._guard:
            other = self._held.get(key)
            if other and other != (env, test_customer):
                return None  # the same customer under another test_customer name in this process: no parallel login
            info = {"test_customer": test_customer, "service": self.service, "environment": env}
            if not self.leases.acquire(key, self.owner, self.profile.session.ttl_s, info):
                return None
            self._held[key] = (env, test_customer)
            return key

    def _release(self, key: str) -> None:
        with self._guard:
            self._held.pop(key, None)
        try:
            self.leases.release(key, self.owner)
        except Exception:  # noqa: BLE001 - never let cleanup hide the real error
            pass

    def _from_source(self, env: EnvironmentConfig, test_customer: CustomerSpec) -> dict[str, Any]:
        step = test_customer.source
        assert step is not None
        op = self.operation(step.operation)
        if op.session:
            raise ProfileError(f"test_customer '{test_customer.name}': source operation '{op.name}' must not need a "
                               "session "
                               "(set session: false in the catalog)")
        result = self._send(env, op, step.params, dict(self.profile.session.defaults), {}, "test_customer", None)
        if not result["business"]["success"]:
            raise ProfileError(f"test_customer '{test_customer.name}': {op.name} failed: "
                               f"{result['business'].get('errors')}")
        out: dict[str, Any] = {}
        for name, path in step.extract.items():
            values = self._find(result.get("body"), path)
            if not values:
                raise ProfileError(f"test_customer '{test_customer.name}': {path} not found in the {op.name} response")
            out[name] = values[0]
        return out

    def _acquire_test_customer(self, env: EnvironmentConfig, name: str) -> tuple[dict[str, Any], str]:
        if name == NO_TEST_CUSTOMER:
            key = self._try_lease(env.name, name, {})
            if key:
                return {}, key
            return self._busy(env, name, [self._lease_key(env.name, {}, name)])
        spec = self._spec(env, name)
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

    def _spec(self, env: EnvironmentConfig, name: str) -> CustomerSpec:
        defined = self.profile.environment(env.name).test_customers
        if name in defined:
            return defined[name]
        return CustomerSpec(name=name, pool=[{self.profile.adhoc_field or "no": name}])

    def _find(self, body: Any, path: str) -> list[Any]:
        if self.profile.dotted_paths:
            from ..tanim.kontrol import coz

            return coz(body, path)
        return find(body, path)

    def _busy(self, env: EnvironmentConfig, name: str, keys: list[str]) -> Any:
        holders = []
        for key in keys:
            holder = self.leases.holder(key)
            local = self._held.get(key)
            if local:
                holders.append({"owner": "this process", "test_customer": local[1]})
            elif holder:
                holders.append(holder)
        raise CustomerBusy(f"Test customer '{name}' ({env.name}) is in use - parallel logins with the same customer "
                          f"are not allowed. Holders: {json.dumps(holders, ensure_ascii=False)}. Wait for the lease "
                          "to end, add customers to its pool, or use another test customer.")

    # -- sessions -------------------------------------------------------------------------------------------------
    def _login(self, env: EnvironmentConfig, st: SessionState) -> None:
        st.vars = dict(self.profile.session.defaults)
        st.logged_in = False
        for step in self.profile.session.login:
            op = self.profile.catalog.get(step.operation)
            if op is None:
                raise LoginFailed(f"login step '{step.operation}' is not in the catalog of '{self.service}'")
            result = self._send(env, op, step.params, st.vars, st.test_customer_vars, "login", st.test_customer)
            if not result["business"]["success"]:
                raise LoginFailed(f"login step {op.name} failed for test_customer '{st.test_customer}' ({env.name}): "
                                  f"{result['business'].get('errors') or result.get('status')}")
            for name, path in step.extract.items():
                values = self._find(result.get("body"), path)
                if not values:
                    raise LoginFailed(f"login step {op.name}: {path} not found in the response")
                st.vars[name] = values[0]
                if isinstance(values[0], str):
                    self.engine.masker.add_secret(values[0])
        st.created = st.last_used = time.time()
        st.logins += 1
        st.logged_in = True

    def _end(self, st: SessionState, logout: bool) -> dict[str, Any]:
        out = {"environment": st.environment, "test_customer": st.test_customer, "logged_out": False}
        if logout and st.logged_in and self.profile.session.logout and st.alive(self.profile):
            try:
                env = self.engine.settings.environment(st.environment)
                step: StepSpec = self.profile.session.logout
                op = self.profile.catalog.get(step.operation)
                if op is not None:
                    res = self._send(env, op, step.params, st.vars, st.test_customer_vars, "logout", st.test_customer)
                    out["logged_out"] = bool(res["business"]["success"])
            except Exception as exc:  # noqa: BLE001
                out["logout_error"] = str(exc)[:300]
        self.sessions.pop((st.environment, st.test_customer), None)
        self._release(st.lease_key)
        return out

    def session(self, env: EnvironmentConfig, test_customer: str | None, fresh: bool = False) -> SessionState:
        name = self._test_customer_name(env, test_customer)
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
            test_customer_vars, lease_key = self._acquire_test_customer(env, name)
            st = SessionState(environment=env.name, test_customer=name, lease_key=lease_key,
                              test_customer_vars=test_customer_vars)
            try:
                self._login(env, st)
            except Exception:
                self._release(lease_key)
                raise
            self.sessions[key] = st
            return st

    # -- public API -----------------------------------------------------------------------------------------------
    def call(self, operation: str, params: dict[str, Any] | None = None, test_customer: str | None = None,
             environment: str | None = None, origin: str = "tool", fresh_session: bool = False) -> dict[str, Any]:
        svc, env = self.engine.resolve(self.service, environment)
        op = self.operation(operation)
        if op.mutating and not env.allow_mutating:
            raise ProfileError(f"'{op.name}' of '{svc.name}' is {op.kind}"
                               + (" (not classified: set kind in the catalog)" if op.kind == "unknown" else "")
                               + f"; environment '{env.name}' has allow_mutating: false.")
        if not op.session:
            has_customers = test_customer or self.profile.environment(env.name).test_customers
            name = self._test_customer_name(env, test_customer) if has_customers \
                else NO_TEST_CUSTOMER
            live = self.sessions.get((env.name, name))
            session_vars = live.vars if live and live.alive(self.profile) else dict(self.profile.session.defaults)
            test_customer_vars = live.test_customer_vars if live else {}
            return self._send(env, op, params, session_vars, test_customer_vars, origin, name if live else None)
        name = self._test_customer_name(env, test_customer)
        with self._key_lock((env.name, name)):
            st = self.session(env, test_customer, fresh_session)
            result = self._send(env, op, params, st.vars, st.test_customer_vars, origin, st.test_customer)
            if self._expired(result):
                st.logged_in = False
                if not op.mutating or self.profile.session.retry_mutating_on_expired:
                    st = self.session(env, test_customer)
                    result = self._send(env, op, params, st.vars, st.test_customer_vars, origin, st.test_customer)
                    result["session_renewed"] = True
                else:
                    result["session_expired"] = ("the server ended the session; this mutating call was not repeated "
                                                 "automatically (retry_mutating_on_expired: false). Call it again "
                                                 "to log in and retry.")
            st.last_used = time.time()
            return result

    def test_customer_values(self, environment: str, test_customer: str | None) -> dict[str, Any]:
        """Fields of the test customer logged in for (environment, test_customer); {{test_customer.x}} in asserts."""
        env = self.engine.settings.environment(environment)
        st = self.sessions.get((env.name, self._test_customer_name(env, test_customer)))
        return dict(st.test_customer_vars) if st else {}

    def end_sessions(self, environment: str | None = None, test_customer: str | None = None,
                     logout: bool = True) -> list[dict[str, Any]]:
        ended = []
        for (env, name), st in list(self.sessions.items()):
            if environment and env != environment:
                continue
            if test_customer and name != test_customer:
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
            for p in self.profile.environment(environment).test_customers.values():
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
