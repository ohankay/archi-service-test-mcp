"""``{{...}}`` placeholders in scenario values.

* ``{{name}}`` / ``{{oturum.token}}`` / ``{{liste[0].no}}`` - scenario variables and values captured by ``extract``.
* ``{{env.name}}`` - ``variables`` of the selected environment in services.yaml (test data per environment).
* ``{{test_customer.Field}}`` / ``{{session.Key}}`` - profile services: the test customer's fields and the login
  session.
  They are filled in by the profile when the request is built, so scenario rendering leaves them as they are.
* ``{{secret.STM_SECRET_X}}`` / ``{{secret.x}}`` - a secret: the OS environment variable ``STM_SECRET_X``, else
  ``x`` from the local gizli.yaml of the environment. Its value is redacted from every output.
* ``{{$uuid}}``, ``{{$now}}``, ``{{$today}}``, ``{{$timestamp}}``, ``{{$random_digits(8)}}``,
  ``{{$random_int(1,100)}}``, ``{{$date(+3)}}`` (today +/- days).

A value that is exactly one placeholder keeps its type (number, boolean, object); otherwise text is substituted.
"""

from __future__ import annotations

import datetime as _dt
import os
import random
import re
import time
import uuid
from typing import Any, Callable

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
SECRET_PREFIX = "STM_SECRET_"


class TemplateError(ValueError):
    pass


def lookup(data: Any, expr: str) -> Any:
    """Dotted / indexed lookup: ``a.b[0].c``. Raises KeyError when missing."""
    current = data
    for name, index in _TOKEN.findall(expr):
        if index:
            if not isinstance(current, (list, tuple)) or int(index) >= len(current):
                raise KeyError(expr)
            current = current[int(index)]
        else:
            if isinstance(current, dict) and name in current:
                current = current[name]
            elif isinstance(current, (list, tuple)) and name.isdigit() and int(name) < len(current):
                current = current[int(name)]
            else:
                raise KeyError(expr)
    return current


class Context:
    def __init__(self, variables: dict[str, Any] | None = None, env_variables: dict[str, Any] | None = None,
                 on_secret: Callable[[str], None] | None = None, keep: tuple[str, ...] = (),
                 secrets: dict[str, Any] | None = None) -> None:
        self.variables: dict[str, Any] = dict(variables or {})
        self.env_variables = dict(env_variables or {})
        self.on_secret = on_secret
        self.keep = keep          # roots left as {{...}} for a later stage (e.g. "test_customer", "session")
        self.secrets = secrets or {}   # local secret store (gizli.yaml) of the environment
        self._depth = 0

    def _kept(self, expr: str) -> bool:
        return bool(self.keep) and expr.strip().split(".")[0].split("[")[0] in self.keep

    def _builtin(self, expr: str) -> Any:
        name, _, args = expr[1:].partition("(")
        args = [a.strip() for a in args.rstrip(")").split(",") if a.strip()] if args else []
        today = _dt.date.today()
        if name == "uuid":
            return str(uuid.uuid4())
        if name == "now":
            return _dt.datetime.now().replace(microsecond=0).isoformat()
        if name == "utcnow":
            return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        if name == "today":
            return today.isoformat()
        if name == "date":
            return (today + _dt.timedelta(days=int(args[0]) if args else 0)).isoformat()
        if name == "timestamp":
            return int(time.time())
        if name == "random_digits":
            n = int(args[0]) if args else 8
            return str(random.randint(1, 9)) + "".join(str(random.randint(0, 9)) for _ in range(n - 1))
        if name == "random_int":
            lo, hi = (int(args[0]), int(args[1])) if len(args) == 2 else (0, 1_000_000)
            return random.randint(lo, hi)
        raise TemplateError(f"Unknown built-in '{{{{{expr}}}}}'. Available: $uuid, $now, $utcnow, $today, "
                            "$date(+/-days), $timestamp, $random_digits(n), $random_int(a,b)")

    def resolve(self, expr: str) -> Any:
        expr = expr.strip()
        if expr.startswith("$"):
            return self._builtin(expr)
        if expr.startswith("env."):
            try:
                value = lookup(self.env_variables, expr[4:])
            except KeyError:
                raise TemplateError(f"'{{{{{expr}}}}}': not in the environment's variables "
                                    f"(available: {', '.join(self.env_variables) or 'none'})") from None
            if isinstance(value, str) and "{{" in value and self._depth < 5:   # e.g. "{{secret.X}}" in a variable
                self._depth += 1
                try:
                    return self.render(value)
                finally:
                    self._depth -= 1
            return value
        if expr.startswith("secret."):
            name = expr[7:]
            env_name = name if name.startswith(SECRET_PREFIX) else SECRET_PREFIX + name.upper()
            value = os.environ.get(env_name)
            if (value is None or value == "") and name in self.secrets:
                value = str(self.secrets[name])
            if not value:
                raise TemplateError(f"'{{{{{expr}}}}}': secret is not set. Set the environment variable {env_name} "
                                    "(in VS Code: the server's env in mcp.json with an ${input:...}) or put "
                                    f"'{name}' into the local gizli.yaml for this environment.")
            if self.on_secret:
                self.on_secret(value)
            return value
        try:
            return lookup(self.variables, expr)
        except KeyError:
            raise TemplateError(f"Unknown variable '{{{{{expr}}}}}'. Defined: "
                                f"{', '.join(sorted(self.variables)) or 'none'}") from None

    def render(self, value: Any) -> Any:
        if isinstance(value, str):
            whole = PLACEHOLDER.fullmatch(value.strip())
            if whole and value.strip() == value:
                return value if self._kept(whole.group(1)) else self.resolve(whole.group(1))
            return PLACEHOLDER.sub(lambda m: m.group(0) if self._kept(m.group(1)) else _text(self.resolve(m.group(1))),
                                   value)
        if isinstance(value, dict):
            return {self.render(k) if isinstance(k, str) else k: self.render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v) for v in value]
        return value


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def placeholders(value: Any) -> list[str]:
    """All placeholder expressions inside a value (for validation)."""
    found: list[str] = []
    if isinstance(value, str):
        found += [m.group(1).strip() for m in PLACEHOLDER.finditer(value)]
    elif isinstance(value, dict):
        for k, v in value.items():
            found += placeholders(k) + placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found += placeholders(v)
    return found
