"""Runs operations and tests written as definitions.

* An operation is called by its friendly name with friendly inputs; defaults, date expressions (bugün-7), formats,
  fixed fields and inputs that come from another operation (``kaynak``) are filled in here; login, session and the
  envelope are the profile's job.
* A test is a list of steps (``adimlar``) or a table (``islem`` + ``ornekler``). Checks are deterministic. The
  expected result of a step or row is ``başarılı`` (default), ``hata`` or ``?``: a ``?`` is not judged, it is
  reported as an observation for a person to confirm (then the test is updated and becomes deterministic).
* ``ai_yorum`` questions are returned with a masked summary of the response for the agent to answer; they never
  decide pass/fail.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..normalize import json_ready
from ..scenario.template import Context, TemplateError
from .degerler import DegerHatasi, donustur
from .depo import Kayit, TanimHatasi
from .derle import sablon
from .kontrol import coz, kontrol_et

if TYPE_CHECKING:
    from ..engine import Engine

_YOK = object()
OZET_KARAKTER = 2500


class Kosucu:
    def __init__(self, engine: "Engine") -> None:
        self.e = engine
        self.depo = engine.depo
        self._kaynak: dict[str, Any] = {}

    # -- helpers ------------------------------------------------------------------------------------------------
    def _ortam(self, ortam: str | None) -> str:
        return self.e.settings.environment(ortam).name

    def _ctx(self, ortam: str, degiskenler: dict[str, Any]) -> Context:
        return Context(variables=degiskenler, env_variables=self.e.env_variables(ortam),
                       on_secret=self.e.masker.add_secret, keep=("test_customer", "session"),
                       secrets=self.e.secrets(ortam))

    def _render(self, value: Any, ortam: str, degiskenler: dict[str, Any]) -> Any:
        return self._ctx(ortam, degiskenler).render(sablon(value))

    def ozet(self, body: Any, limit: int = OZET_KARAKTER) -> Any:
        view = self.e.view(body)
        text = json.dumps(json_ready(view), ensure_ascii=False, default=str)
        return view if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} karakter)"

    # -- one operation ------------------------------------------------------------------------------------------
    def girdileri_hazirla(self, islem: Kayit, girdi: dict[str, Any] | None, musteri: str | None, ortam: str,
                          degiskenler: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """friendly inputs -> (service params, effective friendly values)."""
        tanimlar = islem.veri.get("girdiler") or {}
        verilen = self._render(dict(girdi or {}), ortam, degiskenler)
        bilinmeyen = [k for k in verilen if k not in tanimlar]
        if bilinmeyen:
            raise TanimHatasi([f"'{islem.ad}' işleminde {', '.join(bilinmeyen)} girdisi yok "
                               f"(var olanlar: {', '.join(tanimlar) or '-'})"])
        params: dict[str, Any] = dict(self._render(islem.veri.get("sabit") or {}, ortam, degiskenler))
        etkin: dict[str, Any] = {}
        for ad, tanim in tanimlar.items():
            if ad in verilen:
                value = verilen[ad]
                if value is None:          # explicitly left out (negative tests: missing field)
                    etkin[ad] = None
                    continue
            elif "varsayilan" in tanim:
                value = self._render(tanim["varsayilan"], ortam, degiskenler)
            elif tanim.get("kaynak"):
                value = self._kaynaktan(tanim["kaynak"], musteri, ortam, degiskenler)
            elif tanim.get("zorunlu"):
                raise TanimHatasi([f"'{islem.ad}' için '{ad}' girdisi gerekli"
                                   + (f" ({tanim['aciklama']})" if tanim.get("aciklama") else "")])
            else:
                continue
            if isinstance(value, dict) and set(value) == {"ham"}:
                sent = value["ham"]                       # deliberately unconverted (negative tests)
            else:
                try:
                    sent = donustur(value, str(tanim.get("tip") or "metin"), tanim.get("bicim"))
                except DegerHatasi as exc:
                    raise TanimHatasi([f"'{islem.ad}'.{ad}: {exc}"]) from None
            params[str(tanim["alan"])] = sent
            etkin[ad] = sent
        return params, etkin

    def _kaynaktan(self, kaynak: dict[str, Any], musteri: str | None, ortam: str,
                   degiskenler: dict[str, Any]) -> Any:
        key = json.dumps([kaynak, musteri, ortam], sort_keys=True, default=str)
        if key not in self._kaynak:
            islem = self.depo.gerekli("islem", kaynak["islem"])
            sonuc, _ = self.cagir(islem, kaynak.get("girdi"), musteri, ortam, degiskenler)
            if not (sonuc.get("business") or {}).get("success"):
                raise TanimHatasi([f"'{kaynak['islem']}' (girdi kaynağı) başarısız: "
                                   f"{(sonuc.get('business') or {}).get('errors')}"])
            values = coz(sonuc.get("body"), kaynak["al"])
            if not values:
                raise TanimHatasi([f"'{kaynak['islem']}' yanıtında '{kaynak['al']}' yok"])
            self._kaynak[key] = values[0]
        return self._kaynak[key]

    def cagir(self, islem: Kayit, girdi: dict[str, Any] | None, musteri: str | None, ortam: str,
              degiskenler: dict[str, Any] | None = None, origin: str = "test") -> tuple[dict[str, Any], dict]:
        sistem = self.depo.islem_sistemi(islem.veri)
        params, etkin = self.girdileri_hazirla(islem, girdi, musteri, ortam, degiskenler or {})
        sonuc = self.e.profile_call(sistem.ad, str(islem.veri["metod"]), params,
                                    str(musteri) if musteri not in (None, "") else None, ortam, origin=origin)
        return sonuc, etkin

    def hata_yollari(self, islem: Kayit) -> list[str]:
        return self.e.profiles[self.depo.islem_sistemi(islem.veri).ad].profile.error_paths

    # -- tests --------------------------------------------------------------------------------------------------
    def test_verisi(self, test: str | None = None, icerik: Any = None) -> dict[str, Any]:
        if icerik is not None:
            import yaml

            data = yaml.safe_load(icerik) if isinstance(icerik, str) else icerik
            if not isinstance(data, dict):
                raise TanimHatasi(["test içeriği bir eşleme olmalı"])
            problems = self.depo.dogrula("test", data)
            if problems:
                raise TanimHatasi(problems)
            return data
        if not test:
            raise TanimHatasi(["'test' (ad) veya 'content' (YAML) verin"])
        kayit = self.depo.gerekli("test", test)
        problems = self.depo.dogrula("test", kayit.veri)
        if problems:
            raise TanimHatasi([f"'{kayit.ad}' geçersiz: " + "; ".join(problems)])
        return kayit.veri

    def kos(self, test: str | None = None, icerik: Any = None, musteri: str | None = None,
            ortam: str | None = None, girdiler: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.test_verisi(test, icerik)
        ortam = self._ortam(ortam or data.get("ortam"))
        self._kaynak = {}
        started = time.perf_counter()
        degiskenler: dict[str, Any] = dict(self._render(data.get("degiskenler") or {}, ortam, {}))
        degiskenler.update(girdiler or {})
        rapor: dict[str, Any] = {"test": data["test"], "ortam": ortam, "musteri": musteri, "durum": data.get("durum")}
        if data.get("ornekler"):
            satirlar = self._tablo(data, musteri, ortam, degiskenler)
            rapor["satirlar"] = satirlar
            parcalar = satirlar
        else:
            adimlar = self._adimlar(data, musteri, ortam, degiskenler)
            rapor["adimlar"] = adimlar
            parcalar = adimlar
        say = {s: sum(1 for p in parcalar if p["sonuc"] == s) for s in ("geçti", "kaldı", "gözlem", "hata", "atlandı")}
        rapor["ozet"] = {k: v for k, v in say.items() if v}
        rapor["sonuc"] = "kaldı" if say["kaldı"] or say["hata"] else ("gözlem var" if say["gözlem"] else "geçti")
        gozlem = [p for p in parcalar if p["sonuc"] == "gözlem"]
        if gozlem:
            rapor["onay_bekleyen"] = ("'?' beklenen durumlar gözlem olarak raporlandı. Kullanıcıya sorun: davranış "
                                      "doğru mu? Cevaba göre testte beklenen'i 'başarılı' veya 'hata' yapın "
                                      "(save_definition).")
        sorular = [p.pop("_ai") for p in parcalar if p.get("_ai")]
        if sorular:
            rapor["ai_yorum"] = sorular
        rapor["sure_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return rapor

    def _degerlendir(self, islem: Kayit, beklenen: str, ek_kontroller: list[str], sonuc: dict[str, Any],
                     ortam: str, degiskenler: dict[str, Any]) -> dict[str, Any]:
        if sonuc.get("error"):
            return {"sonuc": "hata", "mesaj": f"servise ulaşılamadı: {sonuc['error'].get('message')}"}
        basarili = bool((sonuc.get("business") or {}).get("success"))
        out: dict[str, Any] = {"cevap": "başarılı" if basarili else "başarısız"}
        if not basarili:
            out["hatalar"] = (sonuc.get("business") or {}).get("errors")
        out["sure_ms"] = sonuc.get("elapsed_ms")
        if beklenen == "?":
            out["sonuc"] = "gözlem"
            return out
        if beklenen == "hata":
            lines = ["başarısız"] + list(ek_kontroller)
        else:
            lines = ["başarılı"] + list(islem.veri.get("kontroller") or []) + list(ek_kontroller)
        try:
            lines = [str(x) for x in self._render(lines, ortam, degiskenler)]
        except TemplateError as exc:
            return {**out, "sonuc": "hata", "mesaj": str(exc)}
        checks = kontrol_et(lines, sonuc, self.hata_yollari(islem))
        failed = [c for c in checks if not c["gecti"]]
        out["sonuc"] = "geçti" if not failed else "kaldı"
        if failed:
            out["kalan_kontroller"] = self.e.view(failed)
            out["yanit_ozeti"] = self.ozet(sonuc.get("body"))
        else:
            out["kontrol_sayisi"] = len(checks)
        return out

    def _adimlar(self, data: dict[str, Any], musteri: str | None, ortam: str,
                 degiskenler: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        dur = False
        for adim in data["adimlar"]:
            entry: dict[str, Any] = {"adim": adim.get("ad") or adim["islem"], "islem": adim["islem"]}
            if dur:
                entry["sonuc"] = "atlandı"
                out.append(entry)
                continue
            try:
                islem = self.depo.gerekli("islem", adim["islem"])
                sonuc, etkin = self.cagir(islem, adim.get("girdi"), musteri, ortam, degiskenler)
            except (TanimHatasi, TemplateError, ValueError) as exc:
                entry.update(sonuc="hata", mesaj=str(exc))
                out.append(entry)
                dur = True
                continue
            entry["girdi"] = self.e.view(etkin)
            yerel = {**degiskenler, **{k: v for k, v in etkin.items() if v is not None}}
            entry.update(self._degerlendir(islem, adim.get("beklenen", "başarılı"), adim.get("kontrol") or [],
                                           sonuc, ortam, yerel))
            for ad, yol in (adim.get("al") or {}).items():
                values = coz(sonuc.get("body"), yol)
                if values:
                    degiskenler[ad] = values[0]
                elif entry["sonuc"] == "geçti":
                    entry.update(sonuc="kaldı", mesaj=f"al: '{yol}' yanıtta yok")
            if adim.get("ai_yorum"):
                entry["_ai"] = {"adim": entry["adim"], "soru": adim["ai_yorum"], "veri": self.ozet(sonuc.get("body"))}
            out.append(entry)
            if entry["sonuc"] in ("kaldı", "hata"):
                dur = True
        return out

    def _tablo(self, data: dict[str, Any], musteri: str | None, ortam: str,
               degiskenler: dict[str, Any]) -> list[dict[str, Any]]:
        islem = self.depo.gerekli("islem", data["islem"])
        out: list[dict[str, Any]] = []
        for row in data["ornekler"]:
            inputs = {**(data.get("ortak_girdi") or {}),
                      **{k: v for k, v in row.items() if k not in ("durum", "beklenen", "kontrol", "not")}}
            entry: dict[str, Any] = {"durum": row["durum"], "beklenen": row["beklenen"]}
            try:
                sonuc, etkin = self.cagir(islem, inputs, musteri, ortam, degiskenler)
            except (TanimHatasi, TemplateError, ValueError) as exc:
                entry.update(sonuc="hata", mesaj=str(exc))
                out.append(entry)
                continue
            entry["girdi"] = self.e.view(etkin)
            yerel = {**degiskenler, **{k: v for k, v in etkin.items() if v is not None}}
            ek = list(data.get("kontrol") or []) + list(row.get("kontrol") or []) if row["beklenen"] == "başarılı" \
                else list(row.get("kontrol") or [])
            entry.update(self._degerlendir(islem, row["beklenen"], ek, sonuc, ortam, yerel))
            if data.get("ai_yorum"):
                entry["_ai"] = {"durum": row["durum"], "soru": data["ai_yorum"], "veri": self.ozet(sonuc.get("body"),
                                                                                                    800)}
            out.append(entry)
        return out
