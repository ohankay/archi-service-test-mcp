"""Traffic log -> draft operation definitions (and, if asked, the user's own secrets into the local gizli.yaml).

The server reads the log; the result for the agent is a summary without values. For each operation of a defined
system that is not yet defined, a draft ``islem`` is proposed:

* fields of the envelope, credentials and customer fields are left out (the system definition supplies them;
  customer fields become ``{{musteri.no}}``),
* dates become inputs with a format and a default relative to the log's day (21.10 in a log of 28.10 -> bugün-7),
* numbers that identify something (account no ...) and free text become inputs with the log's value as default,
* flags (true/false), screen codes (MainResource*) and zero amounts become fixed fields (``sabit``).

The draft is ``taslak``: the user (with the agent) gives it a friendly name, decides which inputs matter and which
checks it needs. Responses are not taken from logs on purpose: some app logs label concurrent responses with the
wrong method; what a response looks like is learned from a real call.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..importer.analyze import Importer, guess_kind
from ..importer.parsers import parse_traffic
from ..normalize import json_ready
from . import gizli
from .depo import TanimHatasi
from .derle import TUR

if TYPE_CHECKING:
    from ..engine import Engine

_TARIH = (("%d.%m.%Y", "GG.AA.YYYY", re.compile(r"^\d{2}\.\d{2}\.\d{4}$")),
          ("%Y-%m-%d", "YYYY-AA-GG", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
          ("%d/%m/%Y", "GG/AA/YYYY", re.compile(r"^\d{2}/\d{2}/\d{4}$")))
_TERS_TUR = {v: k for k, v in TUR.items()}


def _log_gunu(ts: str | None) -> _dt.date | None:
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(str(ts or "")[:19], fmt).date()
        except ValueError:
            continue
    return None


def _girdi_adi(alan: str) -> str:
    """AccountNumber -> account_number (readable, stable; the user may rename it)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", alan).lower()


def _girdi(alan: str, value: Any, gun: _dt.date | None) -> tuple[str, dict[str, Any]] | None:
    """A sample value -> (input name, definition), or None when it belongs to 'sabit'."""
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        return None
    if "resource" in alan.lower():
        return None
    if isinstance(value, str):
        for fmt, bicim, rx in _TARIH:
            if rx.match(value):
                default: Any = value
                try:
                    when = _dt.datetime.strptime(value, fmt).date()
                    if gun:
                        off = (when - gun).days
                        default = "bugün" if off == 0 else f"bugün{off:+d}"
                except ValueError:
                    pass
                return _girdi_adi(alan), {"alan": alan, "tip": "tarih", "bicim": bicim, "varsayilan": default}
        if not value.strip():
            return None
        return _girdi_adi(alan), {"alan": alan, "tip": "metin", "varsayilan": value}
    if isinstance(value, int):
        if value == 0:
            return None
        return _girdi_adi(alan), {"alan": alan, "tip": "sayi", "varsayilan": value}
    return _girdi_adi(alan), {"alan": alan, "tip": "ondalik", "varsayilan": json_ready(value)}


class LogdanTanim:
    def __init__(self, engine: "Engine") -> None:
        self.e = engine
        self.depo = engine.depo

    def calistir(self, path: str | None = None, content: str | None = None, environment: str | None = None,
                 apply: bool = False, save_secrets: bool = False) -> dict[str, Any]:
        sistemler = [k for k in self.depo.hepsi("sistem") if k.ad in self.e.profiles]
        if not sistemler:
            raise TanimHatasi(["Tanımlı (ve geçerli) bir sistem yok. Önce sistem tanımı gerekli "
                               "(tanimlar/sistemler/...)."])
        imp = Importer(self.e)
        if environment:
            self.e.settings.environment(environment)
        text, source = imp._read(path, content)
        fmt, exchanges, warnings = parse_traffic(text)
        bases = [b for name in (k.ad for k in sistemler) for b in imp._bases(name)]
        bases.sort(key=lambda x: -len(x[2]))
        by_op: dict[tuple[str, str], list[Any]] = {}
        envs: Counter = Counter()
        outside: Counter = Counter()
        noise: Counter = Counter()
        order: list[tuple[str, str]] = []
        for ex in exchanges:
            loc = imp._locate(ex, bases, None, None)
            if loc is None:
                from urllib.parse import urlsplit

                parts = urlsplit(ex.url)
                outside[f"{parts.hostname}{'/'.join(parts.path.split('/')[:3])}"] += 1
                continue
            sistem_adi, env, rel = loc
            envs[env] += 1
            profile = self.e.profiles[sistem_adi].profile
            split = imp._split(profile, rel)
            if split is None:
                continue
            op, op_path = split
            if profile.is_noise(op):
                noise[op] += 1
                continue
            key = (sistem_adi, op)
            if key not in by_op:
                order.append(key)
            by_op.setdefault(key, []).append((ex, op_path, env))
        if not by_op and exchanges:
            from urllib.parse import urlsplit

            hosts = sorted({urlsplit(x.url).hostname or "?" for x in exchanges})
            warnings.append("logdaki adresler hiçbir sistemin ortam adresiyle eşleşmedi: " + ", ".join(hosts)
                            + ". Sistem tanımında bu ortamın 'adres'ini kontrol edin.")
        env = environment or (envs.most_common(1)[0][0] if envs else None)
        report: dict[str, Any] = {"format": fmt, "kaynak": source, "istek": len(exchanges),
                                  "ortam": env, "ortamlar": dict(envs)}
        yeni, mevcut, taslaklar = [], [], []
        for sistem_adi, op in order:
            samples = by_op[(sistem_adi, op)]
            sistem = self.depo.gerekli("sistem", sistem_adi)
            profile = self.e.profiles[sistem_adi].profile
            login_ops = {s.operation for s in profile.session.login}
            if op in login_ops or (profile.session.logout and op == profile.session.logout.operation):
                continue
            var = self.depo.bul("islem", op)
            if var is not None:
                known = {g["alan"] for g in (var.veri.get("girdiler") or {}).values()} | set(var.veri.get("sabit")
                                                                                             or {})
                skip = profile.envelope_keys() | set(profile.secret_fields) | set(profile.test_customer_fields)
                body = samples[-1][0].request_body if isinstance(samples[-1][0].request_body, dict) else {}
                new_fields = sorted(k for k in body if k not in known and k not in skip)
                entry = {"islem": var.ad, "metod": op}
                if new_fields:
                    entry["tanimda_olmayan_alanlar"] = new_fields
                mevcut.append(entry)
                continue
            taslak = self._taslak(sistem.ad, op, samples, profile, source)
            taslaklar.append(taslak)
            yeni.append({"metod": op, "onerilen_ad": taslak["ad"], "tur": taslak["tur"],
                         "girdiler": {k: f"{g['tip']}" + (f" (varsayılan {g['varsayilan']})"
                                                          if "varsayilan" in g else "")
                                      for k, g in (taslak.get("girdiler") or {}).items()},
                         "sabit_alan_sayisi": len(taslak.get("sabit") or {})})
        report["yeni_islemler"] = yeni
        if mevcut:
            report["zaten_tanimli"] = mevcut
        if noise:
            report["gurultu_atlanan"] = dict(noise.most_common())
        if outside:
            report["sistem_disi"] = dict(outside.most_common(10))
        warnings.append("Yanıtlar logdan alınmadı: bazı uygulama logları eşzamanlı çağrıların yanıtlarını yanlış "
                        "metoda yazıyor. Yanıt alanları ilk gerçek çağrıda görülür.")
        report["uyarilar"] = warnings
        if save_secrets:
            report["gizli"] = self._gizliler(by_op, exchanges, imp, bases, env)
        if apply:
            saved = []
            for taslak in taslaklar:
                kayit = self.depo.kaydet("islem", taslak)
                saved.append(str(kayit.yol))
            report["kaydedilen"] = saved
            self.e.reload_definitions()
        report["sonraki_adim"] = (
            "Önizleme. Kullanıcıya yeni işlemleri gösterin; Türkçe ad ve hangi girdilerin önemli olduğunu sorun, "
            "onaylarsa apply=true ile taslak olarak kaydedin." if not apply else
            "Taslaklar kaydedildi (durum: taslak). Adlarını ve girdilerini kullanıcıyla netleştirip "
            "save_definition ile güncelleyin; ilk gerçek çağrıyla (call_operation) deneyin.")
        return report

    def _taslak(self, sistem: str, op: str, samples: list[Any], profile: Any, source: str) -> dict[str, Any]:
        ex, op_path, _ = samples[-1]
        body = ex.request_body if isinstance(ex.request_body, dict) else {}
        gun = _log_gunu(ex.timestamp)
        skip = profile.envelope_keys() | set(profile.secret_fields)
        musteri = set(profile.test_customer_fields)
        masker = self.e.masker
        girdiler: dict[str, Any] = {}
        sabit: dict[str, Any] = {}
        for alan, value in body.items():
            if alan in skip or masker._field_kind(alan) == "secret":
                continue
            if alan in musteri:
                sabit[alan] = "{{musteri.no}}"
                continue
            g = _girdi(alan, value, gun)
            if g is None:
                sabit[alan] = json_ready(value)
                continue
            name, tanim = g
            if masker._field_kind(alan) == "partial" or masker.mask(tanim.get("varsayilan")) != \
                    tanim.get("varsayilan"):
                tanim.pop("varsayilan", None)          # personal data is not written into definitions
                tanim["zorunlu"] = True
            girdiler[name] = tanim
        kind = guess_kind(op, set(), None)
        taslak: dict[str, Any] = {"ad": op, "aciklama": f"{source} logundan üretildi; adı ve girdileri gözden "
                                                        "geçirilmeli.",
                                  "sistem": sistem, "metod": op, "yol": op_path,
                                  "tur": _TERS_TUR.get(kind, "bilinmiyor"),
                                  "oturum": True, "durum": "taslak", "kaynak_log": Path(source).name}
        if ex.method and ex.method != "POST":
            taslak["http_metodu"] = ex.method
        if girdiler:
            taslak["girdiler"] = girdiler
        if sabit:
            taslak["sabit"] = sabit
        return taslak

    def _gizliler(self, by_op: dict, exchanges: list[Any], imp: Importer, bases: list, env: str | None) -> Any:
        if not env:
            return {"kaydedilen": [], "not": "ortam bulunamadı; gizli değer kaydedilmedi"}
        names: list[str] = []
        values: dict[str, Any] = {}
        for sistem in self.depo.hepsi("sistem"):
            for ad, kaynak in (sistem.veri.get("gizli_degerler") or {}).items():
                for ex in exchanges:
                    if (ex.name or "") != kaynak["metod"] or not isinstance(ex.request_body, dict):
                        continue
                    loc = imp._locate(ex, bases, None, None)
                    if loc is None or loc[1] != env:
                        continue
                    value = ex.request_body.get(kaynak["alan"])
                    if value not in (None, ""):
                        values[ad] = value
                        self.e.masker.add_secret(str(value), ad, credential=True)
                        names.append(ad)
                        break
        if not values:
            return {"kaydedilen": [], "not": "logda sistem tanımındaki gizli_degerler bulunamadı"}
        path = gizli.kaydet(env, values)
        self.e.reload_secrets()
        return {"kaydedilen": sorted(set(names)), "ortam": env, "dosya": str(path),
                "not": "Değerler yalnızca bu bilgisayardaki gizli.yaml dosyasına yazıldı; hiçbir çıktıda görünmez."}
