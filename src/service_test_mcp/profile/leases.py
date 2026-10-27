"""Persona leases: a test customer is logged in by one holder at a time.

Parallel logins with the same customer are not allowed (a second login ends the first session on the server), and
the holders are separate processes - VS Code windows, standalone agents, CI runs - possibly on separate machines. A
lease is a small JSON file in a directory every holder can see: the local ``%USERPROFILE%\\.service-test-mcp\\locks``
by default, or a shared folder (``locks.dir`` in services.yaml) when several machines share the same customers.

* A lease has an expiry (the session lifetime). Each call renews it; a crashed holder frees it when it runs out.
* Writes are serialized with an ``O_EXCL`` mutex file next to the lease, which also works on SMB/NFS shares.
* ``LeaseStore`` is the interface; a database or Redis implementation can replace ``FileLeaseStore`` later
  without touching profiles or scenarios.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

MUTEX_STALE_S = 15.0


def owner_id() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "unknown"
    return f"{user}@{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseStore(Protocol):
    def acquire(self, key: str, owner: str, ttl_s: float, info: dict[str, Any] | None = None) -> bool: ...

    def renew(self, key: str, owner: str, ttl_s: float) -> bool: ...

    def release(self, key: str, owner: str) -> None: ...

    def holder(self, key: str) -> dict[str, Any] | None: ...


class FileLeaseStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()

    def _paths(self, key: str) -> tuple[Path, Path]:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.directory / f"{digest}.lease", self.directory / f"{digest}.mutex"

    def _lock(self, mutex: Path) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(str(mutex), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return
            except FileExistsError:
                try:
                    if time.time() - mutex.stat().st_mtime > MUTEX_STALE_S:
                        mutex.unlink(missing_ok=True)  # left behind by a crashed process
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"lease mutex {mutex} is busy") from None
                time.sleep(0.02)

    @staticmethod
    def _unlock(mutex: Path) -> None:
        mutex.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def acquire(self, key: str, owner: str, ttl_s: float, info: dict[str, Any] | None = None) -> bool:
        lease, mutex = self._paths(key)
        self._lock(mutex)
        try:
            current = self._read(lease)
            now = time.time()
            if current and current.get("owner") != owner and float(current.get("expires", 0)) > now:
                return False
            self._write(lease, {"key": key, "owner": owner, "acquired": now, "expires": now + ttl_s, **(info or {})})
            return True
        finally:
            self._unlock(mutex)

    def renew(self, key: str, owner: str, ttl_s: float) -> bool:
        lease, mutex = self._paths(key)
        self._lock(mutex)
        try:
            current = self._read(lease)
            now = time.time()
            if not current or current.get("owner") != owner:
                return False
            if float(current.get("expires", 0)) <= now:
                return False  # ran out: someone else may have been told it is free
            current["expires"] = now + ttl_s
            self._write(lease, current)
            return True
        finally:
            self._unlock(mutex)

    def release(self, key: str, owner: str) -> None:
        lease, mutex = self._paths(key)
        self._lock(mutex)
        try:
            current = self._read(lease)
            if current and current.get("owner") == owner:
                lease.unlink(missing_ok=True)
        finally:
            self._unlock(mutex)

    def holder(self, key: str) -> dict[str, Any] | None:
        current = self._read(self._paths(key)[0])
        if not current or float(current.get("expires", 0)) <= time.time():
            return None
        return {"owner": current.get("owner"), "persona": current.get("persona"),
                "expires_in_s": round(float(current["expires"]) - time.time())}
