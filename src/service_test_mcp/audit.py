"""Audit log: one JSON line per service call, bodies masked and truncated.

Default location: ``%USERPROFILE%\\.service-test-mcp\\logs\\audit-YYYYMMDD.jsonl`` (``audit.dir`` or ``STM_AUDIT_DIR``).
Logging must never break a test run, so every error here is swallowed (and reported on stderr).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import home_dir

log = logging.getLogger("service_test_mcp.audit")
_LOCK = threading.Lock()
MAX_FIELD_CHARS = 4000


def _short(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + f"...(+{len(text) - MAX_FIELD_CHARS})"


class AuditLog:
    def __init__(self, enabled: bool = True, directory: str | None = None) -> None:
        env_dir = os.environ.get("STM_AUDIT_DIR")
        self.enabled = enabled and os.environ.get("STM_AUDIT", "1") not in ("0", "false", "off")
        self.directory = Path(env_dir or directory or home_dir() / "logs").expanduser()
        try:
            self.user = getpass.getuser()
        except Exception:  # noqa: BLE001
            self.user = "unknown"

    def write(self, **record: Any) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        entry = {"ts": now.isoformat(timespec="milliseconds"), "user": self.user}
        for key, value in record.items():
            entry[key] = _short(value) if key in ("request", "response") else value
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"audit-{now:%Y%m%d}.jsonl"
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with _LOCK, open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            log.warning("audit log write failed: %s", exc)
