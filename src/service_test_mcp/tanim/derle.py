"""Definitions -> engine: a ``sistem`` becomes a REST service with a profile, its ``islem`` files its catalog.

Templates in definitions use Turkish names; they are mapped to the engine's:

    {{musteri.no}}  -> {{test_customer.no}}      the customer number given for the run
    {{oturum.X}}    -> {{session.X}}             values taken from login responses
    {{ortam.x}}     -> {{env.x}}                 variables of the environment
    {{gizli.x}}     -> {{secret.x}}              STM_SECRET_X or the local gizli.yaml
    {{metod}}       -> {{operation}}             the technical name of the operation being called
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..config import EnvironmentConfig, ServiceConfig
from ..profile.model import Catalog, Operation, Profile, ProfileEnvironment, SessionSpec, StepSpec
from .depo import Depo, Kayit

_NS = (("musteri.", "test_customer."), ("oturum.", "session."), ("ortam.", "env."), ("gizli.", "secret."))
TUR = {"okuma": "read", "degistirme": "mutating", "giris": "auth", "bilinmiyor": "unknown"}


def sablon(value: Any) -> Any:
    """Turkish template names -> engine names, recursively."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            inner = m.group(1).strip()
            if inner == "metod":
                return "{{operation}}"
            for tr, en in _NS:
                if inner.startswith(tr):
                    inner = en + inner[len(tr):]
                    break
            return "{{" + inner + "}}"
        return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", repl, value)
    if isinstance(value, dict):
        return {k: sablon(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sablon(v) for v in value]
    return value


def _adim(adim: dict[str, Any]) -> StepSpec:
    return StepSpec(operation=str(adim["metod"]), params=sablon(adim.get("girdi") or {}),
                    extract={str(k): str(v) for k, v in (adim.get("al") or {}).items()})


def islem_operasyonu(islem: dict[str, Any]) -> Operation:
    return Operation(name=str(islem["metod"]), path=str(islem.get("yol") or "").strip("/"),
                     kind=TUR.get(str(islem.get("tur") or "bilinmiyor"), "unknown"),
                     session=bool(islem.get("oturum", True)),
                     method=str(islem["http_metodu"]).upper() if islem.get("http_metodu") else None,
                     description=str(islem.get("ad") or ""))


def sistem_derle(depo: Depo, kayit: Kayit) -> tuple[ServiceConfig, dict[str, EnvironmentConfig], Profile]:
    v = kayit.veri
    istek = v.get("istek") or {}
    oturum = v.get("oturum") or {}
    musteri_alani = str((v.get("musteri") or {}).get("alan") or "no")
    url = str(istek.get("adres_kalibi") or "{yol}/{metod}").replace("{yol}", "{path}").replace("{metod}",
                                                                                              "{operation}")
    session = SessionSpec(
        ttl_s=float(oturum.get("sure_sn", 300)), refresh_margin_s=float(oturum.get("yenileme_payi_sn", 30)),
        ttl_mode="idle" if oturum.get("sure_turu") == "bosta" else "absolute",
        defaults=dict(oturum.get("baslangic") or {}),
        login=[_adim(a) for a in oturum.get("giris") or []],
        logout=_adim(oturum["cikis"]) if oturum.get("cikis") else None,
        retry_mutating_on_expired=bool(oturum.get("degistirme_tekrar", False)))
    ortak = dict(v.get("degiskenler") or {})
    environments = {}
    envs: dict[str, EnvironmentConfig] = {}
    base_urls = {}
    for name, ortam in (v.get("ortamlar") or {}).items():
        environments[name] = ProfileEnvironment(variables=sablon({**ortak, **(ortam.get("degiskenler") or {})}),
                                                default_customer=str(ortam["varsayilan_musteri"])
                                                if ortam.get("varsayilan_musteri") is not None else None)
        proxy = ortam.get("proxy")
        if isinstance(proxy, str) and proxy.strip().lower() in ("yok", "hayır", "hayir", "none", "direct"):
            proxy = "none"
        envs[name] = EnvironmentConfig(name=name, description=str(ortam.get("aciklama") or ""),
                                       allow_mutating=bool(ortam.get("degistirmeye_izin", True)),
                                       timeout_s=float(ortam.get("zaman_asimi_sn") or 30),
                                       proxy=str(proxy) if proxy else None,
                                       verify_tls=_tls(ortam.get("tls_dogrula", True)))
        base_urls[name] = str(ortam["adres"]).rstrip("/")
    log = v.get("log_alanlari") or {}
    profile = Profile(
        path=kayit.yol or Path(f"{kayit.ad}.yaml"), name=kayit.ad, description=str(v.get("aciklama") or ""),
        method=str(istek.get("metod") or "POST").upper(), url=url,
        envelope=sablon(istek.get("zarf") or {}), session_envelope=sablon(istek.get("oturum_zarfi") or {}),
        headers=sablon(istek.get("basliklar") or {}), error_paths=[str(x) for x in v.get("hata_alanlari") or []],
        session=session, identity=[musteri_alani], environments=environments,
        noise=[str(x) for x in v.get("gurultu") or []],
        test_customer_fields=[str(x) for x in log.get("musteri") or []],
        secret_fields=[str(x) for x in log.get("gizli") or []] + [str(g["alan"]) for g in
                                                                  (v.get("gizli_degerler") or {}).values()],
        success_lines=[str(x) for x in v.get("basari") or []],
        expired_lines=[str(x) for x in oturum.get("bitti_sayilir") or []],
        dotted_paths=True, adhoc_field=musteri_alani, hooks=str(v["kanca"]) if v.get("kanca") else None)
    ops: dict[str, Operation] = {}
    for islem in depo.hepsi("islem"):
        try:
            if depo.islem_sistemi(islem.veri).ad != kayit.ad:
                continue
        except Exception:  # noqa: BLE001 - reported by validation
            continue
        op = islem_operasyonu(islem.veri)
        ops[op.name] = op
    for adim in (oturum.get("giris") or []) + ([oturum["cikis"]] if oturum.get("cikis") else []):
        if adim["metod"] not in ops:
            ops[adim["metod"]] = Operation(name=adim["metod"], path=str(adim.get("yol") or "").strip("/"),
                                           kind="auth", session=adim is oturum.get("cikis"))
    profile.catalog = Catalog(path=None, operations=ops)
    service = ServiceConfig(name=kayit.ad, protocol="rest", description=str(v.get("aciklama") or ""),
                            base_url=base_urls)
    return service, envs, profile


def _tls(value: Any) -> bool | str:
    """true / false, or the path of a CA bundle (.pem); 'evet'/'hayır' accepted too."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("false", "hayır", "hayir", "yok", "0"):
            return False
        if low in ("true", "evet", "1", ""):
            return True
        return os.path.expanduser(os.path.expandvars(value.strip()))
    return bool(value)
