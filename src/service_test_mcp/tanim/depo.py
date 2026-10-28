"""The definitions folder: load, find by name, validate, save.

    tanimlar/
      sistemler/    mobil.yaml          how to reach a system (environments, login, envelope)
      islemler/     hesap-listesi.yaml  one operation with friendly inputs
      yaklasimlar/  sinir-deger.yaml    a test design technique
      testler/      ...yaml             runnable tests

Folder: ``STM_TANIMLAR``, otherwise ``%USERPROFILE%\\.service-test-mcp\\tanimlar``. Every save is validated first
(schema + meaning: referenced operations exist, checks parse ...). A save carries the version (``surum``) it was
based on, so the agent and a UI editing the same file do not overwrite each other.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import home_dir
from .kontrol import KontrolHatasi, ayristir
from .semalar import AD_ALANI, KLASORLER, SEMALAR

_TEMPLATE = re.compile(r"\{\{[^{}]+\}\}")


class TanimHatasi(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def tanim_klasoru() -> Path:
    value = os.environ.get("STM_TANIMLAR")
    if value and not (value.startswith("${") and value.endswith("}")):
        return Path(value).expanduser()
    return home_dir() / "tanimlar"


def slug(name: str) -> str:
    text = str(name).replace("ı", "i").replace("İ", "i")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "tanim"


def _fold(name: str) -> str:
    return slug(name)


def surum(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class Kayit:
    tur: str
    veri: dict[str, Any]
    yol: Path | None
    surum: str = ""

    @property
    def ad(self) -> str:
        return str(self.veri.get(AD_ALANI[self.tur], ""))


@dataclass
class Depo:
    kok: Path
    kayitlar: dict[str, dict[str, Kayit]] = field(default_factory=dict)   # tur -> slug -> Kayit
    yukleme_hatalari: dict[str, list[str]] = field(default_factory=dict)  # dosya -> sorunlar

    @classmethod
    def yukle(cls, kok: Path | None = None) -> "Depo":
        import yaml

        depo = cls(kok=(kok or tanim_klasoru()))
        for tur, klasor in KLASORLER.items():
            depo.kayitlar[tur] = {}
            folder = depo.kok / klasor
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.y*ml")):
                if path.name.startswith((".", "~")):
                    continue
                text = path.read_text(encoding="utf-8-sig")
                try:
                    data = yaml.safe_load(text) or {}
                except Exception as exc:  # noqa: BLE001
                    depo.yukleme_hatalari[str(path)] = [f"YAML okunamadı: {exc}"]
                    continue
                if not isinstance(data, dict):
                    depo.yukleme_hatalari[str(path)] = ["dosya bir eşleme (anahtar: değer) olmalı"]
                    continue
                data = duzelt(tur, data)
                depo.kayitlar[tur][slug(data.get(AD_ALANI[tur]) or path.stem)] = Kayit(tur, data, path, surum(text))
        for tur in KLASORLER:
            for kayit in list(depo.kayitlar[tur].values()):
                problems = depo.dogrula(tur, kayit.veri, kendisi=kayit)
                if problems:
                    depo.yukleme_hatalari[str(kayit.yol)] = problems
        return depo

    # -- lookup -------------------------------------------------------------------------------------------------
    def hepsi(self, tur: str) -> list[Kayit]:
        return list(self.kayitlar.get(tur, {}).values())

    def bul(self, tur: str, ad: str) -> Kayit | None:
        items = self.kayitlar.get(tur, {})
        key = _fold(ad)
        if key in items:
            return items[key]
        for kayit in items.values():
            if tur == "islem" and str(kayit.veri.get("metod", "")).lower() == str(ad).lower():
                return kayit
            if tur == "yaklasim" and key in {_fold(x) for x in kayit.veri.get("diger_adlar") or []}:
                return kayit
        return None

    def gerekli(self, tur: str, ad: str) -> Kayit:
        kayit = self.bul(tur, ad)
        if kayit is None:
            names = ", ".join(k.ad for k in self.hepsi(tur)) or "(yok)"
            label = {"islem": "İşlem", "test": "Test", "sistem": "Sistem", "yaklasim": "Yaklaşım"}[tur]
            hint = {"islem": " Uygulamada bu ekranı kullanıp logu verin, import_log ile tanımlayalım.",
                    "test": " generate_tests ile üretebilir veya save_definition ile yazabilirsiniz."}.get(tur, "")
            raise TanimHatasi([f"{label} '{ad}' tanımlı değil. Tanımlı olanlar: {names}.{hint}"])
        return kayit

    def islem_sistemi(self, islem: dict[str, Any]) -> Kayit:
        if islem.get("sistem"):
            return self.gerekli("sistem", islem["sistem"])
        systems = self.hepsi("sistem")
        if len(systems) == 1:
            return systems[0]
        raise TanimHatasi([f"İşlem '{islem.get('ad')}' hangi sisteme ait? 'sistem:' yazın "
                           f"({', '.join(s.ad for s in systems) or 'sistem tanımı yok'})"])

    # -- validation ---------------------------------------------------------------------------------------------
    def dogrula(self, tur: str, veri: dict[str, Any], kendisi: Kayit | None = None) -> list[str]:
        import jsonschema

        veri = duzelt(tur, veri)
        if tur not in SEMALAR:
            return [f"tanım türü '{tur}' yok; sistem, islem, test veya yaklasim olmalı"]
        problems = []
        validator = jsonschema.Draft202012Validator(SEMALAR[tur])
        for err in sorted(validator.iter_errors(veri), key=lambda e: list(e.absolute_path)):
            where = ".".join(str(p) for p in err.absolute_path) or "(kök)"
            problems.append(f"{where}: {_turkce(err)}")
        if problems:
            return problems
        check = getattr(self, f"_anlam_{tur}")
        return check(veri)

    @staticmethod
    def _kontroller(where: str, lines: list[str] | None) -> list[str]:
        out = []
        for line in lines or []:
            try:
                ayristir(_TEMPLATE.sub("1", str(line)))
            except KontrolHatasi as exc:
                out.append(f"{where}: {exc}")
        return out

    def _anlam_sistem(self, v: dict[str, Any]) -> list[str]:
        out = self._kontroller("basari", v.get("basari"))
        out += self._kontroller("oturum.bitti_sayilir", (v.get("oturum") or {}).get("bitti_sayilir"))
        if any(str(x).strip().lower() in ("başarılı", "basarili", "başarısız") for x in v.get("basari") or []):
            out.append("basari: 'başarılı' kendisini tanımlayamaz; yanıttaki alanı yazın (ör. 'Success = true')")
        for i, adim in enumerate((v.get("oturum") or {}).get("giris") or []):
            if not adim.get("yol"):
                out.append(f"oturum.giris[{i}] ({adim.get('metod')}): 'yol' gerekli (ör. X/XService.svc)")
        for name, env in (v.get("ortamlar") or {}).items():
            if str(name).lower() in ("prod", "production", "prd", "canli", "canlı", "live"):
                out.append(f"ortamlar.{name}: canlı ortam tanımlanamaz")
            if "://" not in str(env.get("adres", "")):
                out.append(f"ortamlar.{name}.adres: http(s):// ile başlayan tam adres olmalı")
        return out

    def _anlam_islem(self, v: dict[str, Any]) -> list[str]:
        out = self._kontroller("kontroller", v.get("kontroller"))
        systems = self.hepsi("sistem")
        if v.get("sistem") and not self.bul("sistem", v["sistem"]):
            out.append(f"sistem: '{v['sistem']}' tanımlı değil")
        elif not v.get("sistem") and len(systems) > 1:
            out.append(f"sistem: birden çok sistem var, hangisi? ({', '.join(s.ad for s in systems)})")
        girdiler = v.get("girdiler") or {}
        for name, g in girdiler.items():
            if g.get("tip") == "liste" and not g.get("secenekler"):
                out.append(f"girdiler.{name}: 'liste' tipi için 'secenekler' gerekli")
            if g.get("tip") == "tarih" and not g.get("bicim"):
                out.append(f"girdiler.{name}: tarih için 'bicim' gerekli (ör. GG.AA.YYYY)")
        aralik = v.get("tarih_araligi")
        if aralik:
            for k in ("baslangic", "bitis"):
                if aralik[k] not in girdiler:
                    out.append(f"tarih_araligi.{k}: '{aralik[k]}' girdilerde yok")
        return out

    def _islem_girdileri(self, where: str, islem_adi: str, girdi: dict[str, Any] | None) -> list[str]:
        islem = self.bul("islem", islem_adi)
        if islem is None:
            return [f"{where}: işlem '{islem_adi}' tanımlı değil (import_log ile tanımlanabilir)"]
        known = set((islem.veri.get("girdiler") or {}))
        unknown = [k for k in (girdi or {}) if k not in known]
        return [f"{where}: '{islem.ad}' işleminde '{k}' girdisi yok (var olanlar: {', '.join(sorted(known)) or '-'})"
                for k in unknown]

    def _anlam_test(self, v: dict[str, Any]) -> list[str]:
        out = self._kontroller("kontrol", v.get("kontrol"))
        for i, adim in enumerate(v.get("adimlar") or []):
            where = f"adimlar[{i}]"
            out += self._islem_girdileri(where, adim["islem"], adim.get("girdi"))
            out += self._kontroller(f"{where}.kontrol", adim.get("kontrol"))
        if v.get("ornekler"):
            out += self._islem_girdileri("ortak_girdi", v["islem"], v.get("ortak_girdi"))
            for i, row in enumerate(v["ornekler"]):
                inputs = {k: x for k, x in row.items() if k not in ("durum", "beklenen", "kontrol", "not")}
                out += self._islem_girdileri(f"ornekler[{i}] ({row.get('durum')})", v["islem"], inputs)
                out += self._kontroller(f"ornekler[{i}].kontrol", row.get("kontrol"))
        if v.get("yaklasim") and not self.bul("yaklasim", v["yaklasim"]):
            out.append(f"yaklasim: '{v['yaklasim']}' tanımlı değil")
        return out

    @staticmethod
    def _anlam_yaklasim(v: dict[str, Any]) -> list[str]:
        if v.get("uretici") == "ajan" and not v.get("kurallar"):
            return ["kurallar: 'ajan' üreticisi için testlerin hangi kurallarla üretileceğini yazın"]
        return []

    # -- save -----------------------------------------------------------------------------------------------------
    def kaydet(self, tur: str, veri: dict[str, Any], temel_surum: str | None = None,
               varsayilan_durum: str = "taslak") -> Kayit:
        import yaml

        if tur not in KLASORLER:
            raise TanimHatasi([f"tanım türü '{tur}' yok; sistem, islem, test veya yaklasim olmalı"])
        veri = duzelt(tur, dict(veri))
        veri.setdefault("durum", varsayilan_durum)
        problems = self.dogrula(tur, veri)
        if problems:
            raise TanimHatasi(problems)
        ad = str(veri[AD_ALANI[tur]])
        mevcut = self.bul(tur, ad)
        if mevcut is not None and mevcut.yol is not None and mevcut.yol.exists():
            current = surum(mevcut.yol.read_text(encoding="utf-8-sig"))
            if temel_surum is None:
                raise TanimHatasi([f"'{ad}' zaten var (sürüm {current}). Güncellemek için okuduğunuz sürümü "
                                   "'surum' olarak verin; yeni bir tanımsa başka bir ad kullanın."])
            if temel_surum != current:
                raise TanimHatasi([f"'{ad}' siz okuduktan sonra değişti (şimdiki sürüm {current}). Tekrar okuyup "
                                   "değişikliği onun üzerine yapın."])
            path = mevcut.yol
        else:
            path = self.kok / KLASORLER[tur] / f"{slug(ad)}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(veri, allow_unicode=True, sort_keys=False, width=120)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        kayit = Kayit(tur, veri, path, surum(text))
        self.kayitlar.setdefault(tur, {})[slug(ad)] = kayit
        return kayit


def _satir(value: Any) -> Any:
    """A check written as 'her X: A <= B' is read by YAML as {'her X': 'A <= B'}; turn it back into the line."""
    if isinstance(value, dict) and len(value) == 1:
        k, v = next(iter(value.items()))
        return f"{k}: {v}"
    return value


def duzelt(tur: str, veri: dict[str, Any]) -> dict[str, Any]:
    """Forgive the usual YAML slips of hand-written definitions (in place on a copy)."""
    import copy

    v = copy.deepcopy(veri)

    def lines(holder: Any, key: str) -> None:
        if isinstance(holder, dict) and isinstance(holder.get(key), list):
            holder[key] = [_satir(x) for x in holder[key]]

    if tur == "sistem":
        lines(v, "basari")
        lines(v.get("oturum"), "bitti_sayilir")
        musteri = v.get("musteri")
        if isinstance(musteri, dict) and musteri.get("alan") is False:      # alan: no -> YAML false
            musteri["alan"] = "no"
    elif tur == "islem":
        lines(v, "kontroller")
    elif tur == "test":
        lines(v, "kontrol")
        for adim in v.get("adimlar") or []:
            lines(adim, "kontrol")
        for row in v.get("ornekler") or []:
            lines(row, "kontrol")
    return v


def _turkce(err: Any) -> str:
    v = err.validator
    if v == "required":
        return f"zorunlu alan eksik: {err.message.split(' ')[0]}"
    if v == "additionalProperties":
        extra = err.message.split("(")[-1].rstrip(")") if "(" in err.message else err.message
        return f"tanınmayan alan: {extra}"
    if v == "enum":
        return f"izin verilen değerler: {', '.join(map(str, err.validator_value))}"
    if v == "type":
        return f"tip {err.validator_value} olmalı"
    if v == "oneOf":
        return "test ya 'adimlar' ya da 'islem' + 'ornekler' (tablo testi) içermeli, ikisi birden değil"
    if v == "minItems":
        return "en az bir öğe olmalı"
    return err.message
