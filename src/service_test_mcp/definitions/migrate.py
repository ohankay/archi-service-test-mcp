"""Converts definitions and secrets of earlier versions (Turkish names) to the current format.

Earlier versions kept definitions in ``%USERPROFILE%\\.service-test-mcp\\tanimlar`` (``sistemler``, ``islemler``,
``testler``, ``yaklasimlar``) with Turkish keys, and secrets in ``gizli.yaml``. ``migrate()`` writes converted copies
into ``definitions`` and ``secrets.yaml`` and leaves the old files where they are (as a backup). Edits the user made
(environment addresses, proxy, timeouts, own operations and tests) are kept; the names of the shipped samples are
mapped to their new English names so references between files still match.

It runs by itself once when the server starts (a marker file in the definitions folder remembers it), and on
``service-test-mcp --migrate``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..config import home_dir
from .secrets import secrets_file
from .store import definitions_folder, slug

MARKER = ".migrated"
FOLDERS = {"sistemler": "system", "islemler": "operation", "testler": "test", "yaklasimlar": "approach"}
NEW_FOLDERS = {"system": "systems", "operation": "operations", "test": "tests", "approach": "approaches"}

# names of the shipped samples
NAMES = {"Mobil Bankacılık": "Mobile Banking", "Ana sayfa": "Home page", "Hesap listesi": "Account list",
         "Hesap hareketleri": "Account transactions", "Kur listesi": "FX rates", "Giriş": "Login",
         "Hesap listeleme": "Account listing", "Hesap hareketleri görüntüleme": "Account transactions view",
         "Kur bilgisi listeleme": "FX rate listing", "Sınır değer analizi": "Boundary value analysis",
         "Eşdeğerlik sınıfları": "Equivalence partitioning", "Negatif testler": "Negative tests",
         "Pairwise (ikili kombinasyon)": "Pairwise", "Kullanıcı yolculuğu": "User journey"}
VARIABLES = {"uygulama_kullanici": "app_user", "uygulama_sifresi": "app_password", "musteri_kullanici": "customer_user",
             "uygulama_sifresi_2": "app_password_2", "musteri_sifresi": "customer_password",
             "kullanici_id": "user_id", "cihaz_id": "device_id", "uygulama_adi": "app_name", "surum": "app_version"}
INPUTS = {"hesap_no": "account_no", "hesap_ek": "account_suffix", "baslangic": "start", "bitis": "end",
          "sadece_tl": "only_tl", "bakiyesizleri_goster": "show_without_balance"}
TAGS = {"duman": "smoke", "giris": "login", "hesap": "account", "kur": "fx"}
STATUS = {"taslak": "draft", "onayli": "approved"}
KIND = {"okuma": "read", "degistirme": "mutating", "giris": "auth", "bilinmiyor": "unknown"}
TYPE = {"metin": "text", "sayi": "integer", "ondalik": "decimal", "tarih": "date", "evet_hayir": "boolean",
        "liste": "list"}
EXPECTED = {"başarılı": "success", "basarili": "success", "hata": "error", "?": "?"}
GENERATOR = {"sinir_deger": "boundary_value", "esdeger": "equivalence", "pairwise": "pairwise",
             "negatif": "negative", "ajan": "agent"}
GENERATED_BY = {"elle": "manual", "otomatik": "code", "ajan": "agent"}


def legacy_folder() -> Path:
    value = os.environ.get("STM_TANIMLAR")
    return Path(value).expanduser() if value and not value.startswith("${") else home_dir() / "tanimlar"


def legacy_secrets_file() -> Path:
    value = os.environ.get("STM_GIZLI")
    return Path(value).expanduser() if value and not value.startswith("${") else home_dir() / "gizli.yaml"


# -- values -----------------------------------------------------------------------------------------------------------
def _template(match: re.Match) -> str:
    inner = match.group(1).strip()
    if inner == "metod":
        return "{{method}}"
    for old, new in (("musteri.", "customer."), ("oturum.", "session."), ("ortam.", "env."), ("gizli.", "secret.")):
        if inner.startswith(old):
            rest = inner[len(old):]
            if new in ("env.", "secret."):
                rest = VARIABLES.get(rest, rest)
            return "{{" + new + rest + "}}"
    return "{{" + INPUTS.get(inner, inner) + "}}"


def text(value: str) -> str:
    """Templates and day expressions inside a string."""
    out = re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", _template, value)
    return re.sub(r"\bbug[üu]n(?=\s*(?:[+-]\s*\d+)?(?:\W|$))", "today", out)


def date_format(fmt: Any) -> Any:
    """GG.AA.YYYY -> DD.MM.YYYY (earlier: SS hour, DD minute -> HH, mm)."""
    if not isinstance(fmt, str):
        return fmt
    return fmt.replace("DD", "mm").replace("SS", "HH").replace("GG", "DD").replace("AA", "MM")


def value(v: Any) -> Any:
    if isinstance(v, str):
        return text(v)
    if isinstance(v, dict):
        if set(v) == {"ham"}:
            return {"raw": value(v["ham"])}
        return {k: value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [value(x) for x in v]
    return v


def check(line: Any) -> Any:
    """A check line of the earlier (Turkish) language -> the current one."""
    if isinstance(line, dict) and len(line) == 1:        # "her X: ..." read by YAML as a mapping
        k, v = next(iter(line.items()))
        line = f"{k}: {v}"
    if not isinstance(line, str):
        return line
    t = text(line.strip())
    low = t.lower()
    if low in ("başarılı", "basarili", "başarılı olmalı"):
        return "success"
    if low in ("başarısız", "basarisiz", "hata", "hata vermeli"):
        return "failure"
    m = re.match(r"^s[üu]re\s*(<=|<|>=|>)\s*(\d+(?:[.,]\d+)?)\s*(sn|saniye|ms)?$", low)
    if m:
        unit = {"sn": " s", "saniye": " s", "ms": " ms", None: ""}[m.group(3)]
        return f"duration {m.group(1)} {m.group(2)}{unit}"
    prefix = ""
    m = re.match(r"^her\s+(\S+)\s+i[çc]in\s+(.+)$", t, re.IGNORECASE) or re.match(r"^her\s+([^:]+):\s*(.+)$", t,
                                                                                    re.IGNORECASE)
    if m:
        prefix, t = f"for each {m.group(1).strip()}: ", m.group(2).strip()
    m = re.match(r"^hata\s+kodu\s+(=|içerir|icerir)\s+(.+)$", t, re.IGNORECASE)
    if m:
        return prefix + f"error code {'=' if m.group(1) == '=' else 'contains'} {m.group(2)}"
    m = re.match(r"^(\S+)\s+say[ıi]s[ıi]\s*(.+)$", t, re.IGNORECASE)
    if m:
        t = f"count {m.group(1)} {m.group(2)}"
    for old, new in ((r"\s+bo[şs]\s+de[ğg]il$", " is not empty"), (r"\s+bo[şs]$", " is empty"),
                     (r"\s+var$", " exists"), (r"\s+yok$", " missing"), (r"\s+i[çc]ermez\s+", " does not contain "),
                     (r"\s+i[çc]erir\s+", " contains "), (r"\s+e[şs]le[şs]ir\s+", " matches ")):
        t = re.sub(old, new, t, flags=re.IGNORECASE)
    m = re.match(r"^(.+?)\s+aras[ıi]nda\s+(.+?)\s+ve\s+(.+)$", t, re.IGNORECASE)
    if m:
        t = f"{m.group(1)} between {m.group(2)} and {m.group(3)}"
    return prefix + t


def _checks(lines: Any) -> Any:
    return [check(x) for x in lines] if isinstance(lines, list) else lines


def _keys(data: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    return {mapping.get(k, k): v for k, v in data.items()}


def _name(v: Any) -> Any:
    return NAMES.get(v, v) if isinstance(v, str) else v


# -- kinds ------------------------------------------------------------------------------------------------------------
def _step(s: dict[str, Any]) -> dict[str, Any]:
    out = _keys(s, {"metod": "method", "yol": "path", "girdi": "input", "aciklama": "description", "al": "extract"})
    if "input" in out:
        out["input"] = value(out["input"])
    return out


def system(v: dict[str, Any]) -> dict[str, Any]:
    out = _keys(v, {"ad": "name", "aciklama": "description", "durum": "status", "ortamlar": "environments",
                    "degiskenler": "variables", "istek": "request", "basari": "success",
                    "hata_alanlari": "error_fields", "oturum": "session", "musteri": "customer", "gurultu": "noise",
                    "gizli_degerler": "secret_values", "log_alanlari": "log_fields", "kanca": "hook"})
    out["name"] = _name(out.get("name"))
    out["status"] = STATUS.get(out.get("status"), out.get("status"))
    envs = {}
    for name, env in (out.get("environments") or {}).items():
        e = _keys(env, {"adres": "address", "aciklama": "description", "degiskenler": "variables",
                        "degistirmeye_izin": "allow_mutating", "varsayilan_musteri": "default_customer",
                        "zaman_asimi_sn": "timeout_s", "tls_dogrula": "verify_tls"})
        if "variables" in e:
            e["variables"] = {VARIABLES.get(k, k): value(x) for k, x in (e["variables"] or {}).items()}
        proxy = str(e.get("proxy", "")).strip().lower()
        if proxy in ("yok", "hayır", "hayir"):
            e["proxy"] = "none"
        elif proxy in ("otomatik",):
            e["proxy"] = "auto"
        envs[name] = e
    out["environments"] = envs
    if "variables" in out:
        out["variables"] = {VARIABLES.get(k, k): value(x) for k, x in (out["variables"] or {}).items()}
    if "request" in out:
        r = _keys(out["request"], {"metod": "method", "adres_kalibi": "url_pattern", "zarf": "envelope",
                                   "oturum_zarfi": "session_envelope", "basliklar": "headers"})
        if "url_pattern" in r:
            r["url_pattern"] = str(r["url_pattern"]).replace("{yol}", "{path}").replace("{metod}", "{method}")
        out["request"] = {k: value(x) for k, x in r.items()}
    out["success"] = _checks(out.get("success")) if "success" in out else None
    if out["success"] is None:
        out.pop("success")
    if "session" in out:
        s = _keys(out["session"], {"sure_sn": "ttl_s", "yenileme_payi_sn": "refresh_margin_s", "sure_turu": "ttl_mode",
                                   "baslangic": "defaults", "giris": "login", "cikis": "logout",
                                   "bitti_sayilir": "expired_when", "degistirme_tekrar": "retry_mutating"})
        if "ttl_mode" in s:
            s["ttl_mode"] = {"mutlak": "absolute", "bosta": "idle"}.get(s["ttl_mode"], s["ttl_mode"])
        if "login" in s:
            s["login"] = [_step(x) for x in s["login"] or []]
        if s.get("logout"):
            s["logout"] = _step(s["logout"])
        if "expired_when" in s:
            s["expired_when"] = _checks(s["expired_when"])
        out["session"] = s
    if "customer" in out and isinstance(out["customer"], dict):
        out["customer"] = _keys(out["customer"], {"alan": "field"})
    if "secret_values" in out:
        out["secret_values"] = {VARIABLES.get(k, k): _keys(x, {"metod": "method", "alan": "field"})
                                for k, x in (out["secret_values"] or {}).items()}
    if "log_fields" in out:
        out["log_fields"] = _keys(out["log_fields"], {"musteri": "customer", "gizli": "secret"})
    return out


def operation(v: dict[str, Any]) -> dict[str, Any]:
    out = _keys(v, {"ad": "name", "aciklama": "description", "sistem": "system", "metod": "method", "yol": "path",
                    "http_metodu": "http_method", "tur": "kind", "oturum": "session", "durum": "status",
                    "etiketler": "tags", "girdiler": "inputs", "sabit": "fixed", "tarih_araligi": "date_range",
                    "kontroller": "checks", "kaynak_log": "source_log"})
    out["name"] = _name(out.get("name"))
    if "system" in out:
        out["system"] = _name(out["system"])
    if "kind" in out:
        out["kind"] = KIND.get(out["kind"], out["kind"])
    if "status" in out:
        out["status"] = STATUS.get(out["status"], out["status"])
    if "tags" in out:
        out["tags"] = [TAGS.get(t, t) for t in out["tags"] or []]
    if "inputs" in out:
        inputs = {}
        for name, spec in (out["inputs"] or {}).items():
            i = _keys(spec, {"alan": "field", "tip": "type", "zorunlu": "required", "varsayilan": "default",
                             "bicim": "format", "aciklama": "description", "en_az": "min", "en_fazla": "max",
                             "secenekler": "options", "kaynak": "source"})
            if "type" in i:
                i["type"] = TYPE.get(i["type"], i["type"])
            if "default" in i:
                d = value(i["default"])
                if i.get("type") == "boolean" and isinstance(d, str):
                    d = {"evet": True, "hayır": False, "hayir": False}.get(d.strip().lower(), d)
                i["default"] = d
            for key in ("min", "max"):
                if key in i:
                    i[key] = value(i[key])
            if "format" in i:
                i["format"] = date_format(i["format"])
            if isinstance(i.get("source"), dict):
                src = _keys(i["source"], {"islem": "operation", "al": "extract", "girdi": "input"})
                src["operation"] = _name(src.get("operation"))
                i["source"] = src
            inputs[INPUTS.get(name, name)] = i
        out["inputs"] = inputs
    if "fixed" in out:
        out["fixed"] = value(out["fixed"])
    if isinstance(out.get("date_range"), dict):
        d = _keys(out["date_range"], {"baslangic": "start", "bitis": "end", "en_fazla_gun": "max_days"})
        for key in ("start", "end"):
            if key in d:
                d[key] = INPUTS.get(d[key], d[key])
        out["date_range"] = d
    if "checks" in out:
        out["checks"] = _checks(out["checks"])
    return out


def _example(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, x in row.items():
        key = {"durum": "case", "beklenen": "expected", "kontrol": "checks", "not": "note"}.get(k) or INPUTS.get(k, k)
        if key == "expected":
            x = EXPECTED.get(x, x)
        elif key == "checks":
            x = _checks(x)
        elif key not in ("case", "note"):
            x = value(x)
        out[key] = x
    return out


def test(v: dict[str, Any]) -> dict[str, Any]:
    out = _keys(v, {"test": "name", "aciklama": "description", "etiketler": "tags", "durum": "status",
                    "ortam": "environment", "degiskenler": "variables", "adimlar": "steps", "islem": "operation",
                    "ornekler": "examples", "ortak_girdi": "common_input", "kontrol": "checks",
                    "ai_yorum": "ai_review", "yaklasim": "approach", "uretim": "generated_by"})
    out["name"] = _name(out.get("name"))
    if "status" in out:
        out["status"] = STATUS.get(out["status"], out["status"])
    if "tags" in out:
        out["tags"] = [TAGS.get(t, t) for t in out["tags"] or []]
    if "variables" in out:
        out["variables"] = {INPUTS.get(k, k): value(x) for k, x in (out["variables"] or {}).items()}
    steps = []
    for s in out.get("steps") or []:
        st = _keys(s, {"ad": "name", "islem": "operation", "girdi": "input", "kontrol": "checks",
                       "ai_yorum": "ai_review", "al": "extract", "beklenen": "expected"})
        st["operation"] = _name(st.get("operation"))
        if "input" in st:
            st["input"] = {INPUTS.get(k, k): value(x) for k, x in (st["input"] or {}).items()}
        if "extract" in st:
            st["extract"] = {INPUTS.get(k, k): x for k, x in (st["extract"] or {}).items()}
        if "checks" in st:
            st["checks"] = _checks(st["checks"])
        if "expected" in st:
            st["expected"] = EXPECTED.get(st["expected"], st["expected"])
        steps.append(st)
    if steps:
        out["steps"] = steps
    if "operation" in out:
        out["operation"] = _name(out["operation"])
    if "examples" in out:
        out["examples"] = [_example(r) for r in out["examples"] or []]
    if "common_input" in out:
        out["common_input"] = {INPUTS.get(k, k): value(x) for k, x in (out["common_input"] or {}).items()}
    if "checks" in out:
        out["checks"] = _checks(out["checks"])
    if "approach" in out:
        out["approach"] = _name(out["approach"])
    if "generated_by" in out:
        out["generated_by"] = GENERATED_BY.get(out["generated_by"], out["generated_by"])
    return out


def approach(v: dict[str, Any]) -> dict[str, Any]:
    out = _keys(v, {"yaklasim": "name", "diger_adlar": "aliases", "aciklama": "description", "ne_zaman": "when_to_use",
                    "durum": "status", "uretici": "generator", "kurallar": "rules", "sinir": "limit",
                    "beklenen_disari": "expected_outside"})
    old_name = out.get("name")
    out["name"] = _name(old_name)
    if old_name != out["name"]:
        out["aliases"] = list(dict.fromkeys([*(out.get("aliases") or []), old_name]))
    if "status" in out:
        out["status"] = STATUS.get(out["status"], out["status"])
    if "generator" in out:
        out["generator"] = GENERATOR.get(out["generator"], out["generator"])
    if "expected_outside" in out:
        out["expected_outside"] = EXPECTED.get(out["expected_outside"], out["expected_outside"])
    return out


CONVERT = {"system": system, "operation": operation, "test": test, "approach": approach}


# -- files ------------------------------------------------------------------------------------------------------------
def migrate(force: bool = False) -> dict[str, Any]:
    """Convert the earlier definitions folder and secrets file, once (or again with force)."""
    import yaml

    old = legacy_folder()
    new = definitions_folder()
    report: dict[str, Any] = {"from": str(old), "to": str(new)}
    if old.resolve() == new.resolve():
        return {**report, "migrated": False, "reason": "the definitions folder is the earlier one"}
    marker = new / MARKER
    if marker.exists() and not force:
        return {**report, "migrated": False, "reason": "already migrated"}
    written, problems = [], []
    if old.is_dir():
        for folder, kind in FOLDERS.items():
            for path in sorted((old / folder).glob("*.y*ml")) if (old / folder).is_dir() else []:
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
                    if not isinstance(data, dict):
                        raise ValueError("not a mapping")
                    converted = CONVERT[kind](data)
                    dest = new / NEW_FOLDERS[kind] / f"{slug(converted.get('name') or path.stem)}.yaml"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(yaml.safe_dump(converted, allow_unicode=True, sort_keys=False, width=120),
                                    encoding="utf-8")
                    written.append(str(dest))
                except Exception as exc:  # noqa: BLE001 - reported, the file is left for the user
                    problems.append(f"{path}: {exc}")
    report["secrets"] = _migrate_secrets()
    if not written and not old.is_dir() and not report["secrets"].get("written"):
        return {**report, "migrated": False, "reason": "no earlier definitions"}
    new.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"migrated from {old}\n", encoding="utf-8")
    report.update(migrated=True, written=written,
                  note="The earlier files were left in place as a backup; they are no longer read.")
    if problems:
        report["problems"] = problems
    return report


def _migrate_secrets() -> dict[str, Any]:
    import yaml

    old = legacy_secrets_file()
    new = secrets_file()
    if not old.exists():
        return {"written": False}
    data = yaml.safe_load(old.read_text(encoding="utf-8-sig")) or {}
    converted = {str(env): {VARIABLES.get(k, k): v for k, v in (values or {}).items()}
                 for env, values in data.items() if isinstance(values, dict)}
    current = yaml.safe_load(new.read_text(encoding="utf-8-sig")) or {} if new.exists() else {}
    for env, values in converted.items():   # values already in secrets.yaml win
        current[env] = {**values, **(current.get(env) or {})}
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("# Local secret values - do not share, do not commit.\n"
                   + yaml.safe_dump(current, allow_unicode=True, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(new, 0o600)
    except OSError:
        pass
    return {"written": True, "file": str(new), "names": sorted({k for v in converted.values() for k in v})}
