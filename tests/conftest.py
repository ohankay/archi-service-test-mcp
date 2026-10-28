"""Shared fixtures: the local demo services on a free port and a services.yaml pointing to them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _masked_by_default(monkeypatch):
    """Most tests check masking; the transparent default (STM_SEFFAF=seffaf) has its own tests."""
    monkeypatch.setenv("STM_SEFFAF", "maskeli")
    monkeypatch.delenv("STM_GIZLILERI_GOSTER", raising=False)
DEMO = ROOT / "examples" / "demo"


@pytest.fixture(scope="session")
def demo_port():
    from service_test_mcp import demo

    server = demo.start(0)
    yield server.server_port
    server.shutdown()


def write_config(tmp: Path, port: int, extra: str = "") -> Path:
    text = (DEMO / "services.yaml").read_text(encoding="utf-8").replace("127.0.0.1:8899", f"127.0.0.1:{port}")
    cfg = tmp / "services.yaml"
    cfg.write_text(text + extra, encoding="utf-8")
    (tmp / "schemas").mkdir(exist_ok=True)
    (tmp / "schemas" / "hesap-detay.json").write_text((DEMO / "schemas" / "hesap-detay.json").read_text("utf-8"),
                                                       encoding="utf-8")
    scen = tmp / "scenarios"
    scen.mkdir(exist_ok=True)
    for f in (DEMO / "scenarios").glob("*.yaml"):
        (scen / f.name).write_text(f.read_text("utf-8"), encoding="utf-8")
    return cfg


@pytest.fixture()
def demo_config(tmp_path, demo_port, monkeypatch):
    cfg = write_config(tmp_path, demo_port)
    monkeypatch.setenv("STM_CONFIG", str(cfg))
    monkeypatch.setenv("STM_HOME", str(tmp_path / "home"))
    return cfg


@pytest.fixture()
def engine(demo_config):
    from service_test_mcp.engine import Engine

    e = Engine()
    assert e.load_error is None, e.load_error
    yield e
    e.close()


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STM_")}
    env["STM_TOOLSET"] = "uzman"          # every tool; tests of the default set pass STM_TOOLSET="temel"
    env["STM_SEFFAF"] = "maskeli"
    env.update(extra)
    return env


def write_tanimlar(tmp: Path, port: int) -> Path:
    """The packaged ready definitions, pointed at the in-process demo service."""
    import shutil

    src = ROOT / "src" / "service_test_mcp" / "ornek_tanimlar"
    target = tmp / "tanimlar"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src, target)
    system = target / "sistemler" / "mobil-bankacilik.yaml"
    system.write_text(system.read_text("utf-8").replace(":8899", f":{port}"), encoding="utf-8")
    return target


@pytest.fixture()
def tanimlar(tmp_path, demo_port, monkeypatch):
    from service_test_mcp import demo_mobile

    demo_mobile.reset()
    target = write_tanimlar(tmp_path, demo_port)
    monkeypatch.setenv("STM_TANIMLAR", str(target))
    monkeypatch.setenv("STM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("STM_CONFIG", raising=False)
    monkeypatch.delenv("STM_LOCKS_DIR", raising=False)
    monkeypatch.delenv("STM_GIZLI", raising=False)
    return target


@pytest.fixture()
def tanim_engine(tanimlar):
    from service_test_mcp.engine import Engine

    e = Engine()
    assert e.load_error is None and not e.settings.broken and not e.depo.yukleme_hatalari, \
        (e.load_error, e.settings.broken, e.depo.yukleme_hatalari)
    yield e
    e.close()


MOBILE = ROOT / "examples" / "mobile"


def write_mobile_config(tmp: Path, port: int, extra: str = "", allow_mutating: bool = True) -> Path:
    """Copy examples/mobile into tmp, pointing to the in-process demo service on ``port``."""
    import shutil

    target = tmp / "mobile"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(MOBILE, target)
    cfg = target / "services.yaml"
    text = cfg.read_text("utf-8").replace(":8899", f":{port}")
    if not allow_mutating:
        text = text.replace("allow_mutating: true", "allow_mutating: false")
    cfg.write_text(text + extra, encoding="utf-8")
    log = target / "logs" / "ornek-prep-transaction.log"
    log.write_text(log.read_text("utf-8").replace(":8899", f":{port}"), encoding="utf-8")
    return cfg


@pytest.fixture()
def mobile_config(tmp_path, demo_port, monkeypatch):
    from service_test_mcp import demo_mobile

    demo_mobile.reset()
    cfg = write_mobile_config(tmp_path, demo_port)
    monkeypatch.setenv("STM_CONFIG", str(cfg))
    monkeypatch.setenv("STM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("STM_LOCKS_DIR", raising=False)
    return cfg


@pytest.fixture()
def mobile(mobile_config):
    from service_test_mcp.engine import Engine

    e = Engine()
    assert e.load_error is None and not e.settings.broken, (e.load_error, e.settings.broken)
    yield e
    e.close()
