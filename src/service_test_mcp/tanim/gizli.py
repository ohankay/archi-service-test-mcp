"""Local secret store: ``%USERPROFILE%\\.service-test-mcp\\gizli.yaml`` (or ``STM_GIZLI``).

    prep:
      musteri_sifresi: ...
      otp: ...
    "*":                      # every environment
      uygulama_sifresi: ...

Used by ``{{gizli.x}}`` / ``{{secret.x}}`` when no ``STM_SECRET_X`` environment variable is set. It lives only on
the user's machine (never in the definitions folder or a repository); values never appear in any output.
``import_log(save_secrets=true)`` fills it from the user's own log.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config import home_dir


def gizli_dosyasi() -> Path:
    value = os.environ.get("STM_GIZLI")
    if value and not (value.startswith("${") and value.endswith("}")):
        return Path(value).expanduser()
    return home_dir() / "gizli.yaml"


def oku() -> dict[str, dict[str, Any]]:
    import yaml

    path = gizli_dosyasi()
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def ortam_icin(data: dict[str, dict[str, Any]], ortam: str) -> dict[str, Any]:
    return {**data.get("*", {}), **data.get(ortam, {})}


def kaydet(ortam: str, degerler: dict[str, Any]) -> Path:
    import yaml

    data = oku()
    data.setdefault(ortam, {}).update({k: v for k, v in degerler.items() if v not in (None, "")})
    path = gizli_dosyasi()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Yerel gizli degerler - paylasmayin, repoya koymayin.\n"
                   + yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
