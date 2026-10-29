"""Local secret store: ``%USERPROFILE%\\.service-test-mcp\\secrets.yaml`` (or ``STM_SECRETS_FILE``).

    prep:
      customer_password: ...
      otp: ...
    "*":                      # every environment
      app_password: ...

Used by ``{{secret.x}}`` when no ``STM_SECRET_X`` environment variable is set. It lives only on the user's machine
(never in the definitions folder or a repository); values never appear in any output.
``import_log(save_secrets=true)`` fills it from the user's own log.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import env_setting, home_dir


def secrets_file() -> Path:
    value = env_setting("STM_SECRETS_FILE")
    return Path(value).expanduser() if value else home_dir() / "secrets.yaml"


def read() -> dict[str, dict[str, Any]]:
    import yaml

    path = secrets_file()
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def for_environment(data: dict[str, dict[str, Any]], environment: str) -> dict[str, Any]:
    return {**data.get("*", {}), **data.get(environment, {})}


def save(environment: str, values: dict[str, Any]) -> Path:
    import yaml

    data = read()
    data.setdefault(environment, {}).update({k: v for k, v in values.items() if v not in (None, "")})
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Local secret values - do not share, do not commit.\n"
                   + yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
