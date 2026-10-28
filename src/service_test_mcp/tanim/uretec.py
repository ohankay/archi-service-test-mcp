"""Test generators of the test approaches. Deterministic: same operation definition -> same table.

* ``sinir_deger``  boundary values of every limited input (en_az / en_fazla, and the date range rule)
* ``esdeger``      one representative of each equivalence class (valid options, invalid option, past/today/future ...)
* ``negatif``      required inputs left out, wrong types sent as they are
* ``pairwise``     every pair of input values covered at least once (greedy all-pairs)
* ``ajan``         no code: the approach's rules are handed to the agent, which writes the table itself

The result is a draft table test (``durum: taslak``). Expected results come from the definition (inside a limit ->
başarılı, outside -> the approach's ``beklenen_disari``); what the definition can not tell is ``?`` and is reported as
an observation for a person to confirm.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .degerler import gun_ifadesi
from .depo import Depo, Kayit, TanimHatasi

VARSAYILAN_SINIR = 40


def _gun(expr: Any) -> int | None:
    """'bugün-30' -> -30 (days from today), else None."""
    day = gun_ifadesi(expr)
    if day is None:
        return None
    import datetime as _dt

    return (day - _dt.date.today()).days


def _gun_ifadesi(offset: int) -> str:
    return "bugün" if offset == 0 else f"bugün{offset:+d}"


def _adim(tip: str) -> Decimal:
    return Decimal("0.01") if tip == "ondalik" else Decimal(1)


def _sayi(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _out(value: Decimal, tip: str) -> Any:
    return int(value) if tip == "sayi" else str(value)


class Uretec:
    def __init__(self, depo: Depo) -> None:
        self.depo = depo

    def uret(self, yaklasim: str, islem: str, ortak_girdi: dict[str, Any] | None = None) -> dict[str, Any]:
        yk = self.depo.gerekli("yaklasim", yaklasim)
        ik = self.depo.gerekli("islem", islem)
        uretici = yk.veri["uretici"]
        notlar: list[str] = []
        if uretici == "ajan":
            return {"uretim": "ajan", "yaklasim": yk.ad, "islem": ik.ad,
                    "kurallar": yk.veri.get("kurallar") or [], "girdiler": ik.veri.get("girdiler") or {},
                    "talimat": "Bu yaklaşım kodla üretilmiyor. Kurallara göre bir tablo testi (islem + ornekler) "
                               "yazın, beklenen sonucunu bilmediğiniz satırlara '?' koyun, kullanıcıya gösterin ve "
                               "onaylanırsa save_definition ile kaydedin."}
        rows = getattr(self, f"_{uretici}")(ik, yk, notlar)
        rows = _tekil(rows)
        sinir = int(yk.veri.get("sinir") or VARSAYILAN_SINIR)
        if len(rows) > sinir:
            notlar.append(f"{len(rows)} durum üretildi, yaklaşımın sınırı {sinir}: ilk {sinir} durum alındı")
            rows = rows[:sinir]
        if not rows:
            raise TanimHatasi([f"'{ik.ad}' için '{yk.ad}' durumu üretilemedi: " + ("; ".join(notlar) or
                                                                                 "girdi bilgisi yetersiz")])
        test: dict[str, Any] = {"test": f"{ik.ad} - {yk.ad}", "aciklama": f"'{yk.ad}' yaklaşımıyla otomatik üretildi.",
                                "durum": "taslak", "yaklasim": yk.ad, "uretim": "otomatik", "islem": ik.ad}
        if ortak_girdi:
            test["ortak_girdi"] = ortak_girdi
        test["ornekler"] = rows
        return {"test": test, "notlar": notlar,
                "bilinmeyen_beklenen": sum(1 for r in rows if r["beklenen"] == "?")}

    # -- generators ----------------------------------------------------------------------------------------------
    def _sinir_deger(self, ik: Kayit, yk: Kayit, notlar: list[str]) -> list[dict[str, Any]]:
        disari = yk.veri.get("beklenen_disari", "hata")
        rows: list[dict[str, Any]] = []
        for ad, g in (ik.veri.get("girdiler") or {}).items():
            tip = g.get("tip") or "metin"
            lo, hi = g.get("en_az"), g.get("en_fazla")
            if lo is None and hi is None:
                continue
            if tip in ("sayi", "ondalik"):
                step = _adim(tip)
                for label, bound, sign in (("en az", lo, -1), ("en fazla", hi, 1)):
                    b = _sayi(bound)
                    if b is None:
                        continue
                    rows += [{"durum": f"{ad}: {label}{'-' if sign < 0 else '+'}1 (sınır dışı)",
                              ad: _out(b + sign * step, tip), "beklenen": disari},
                             {"durum": f"{ad}: {label} ({_out(b, tip)})", ad: _out(b, tip), "beklenen": "başarılı"},
                             {"durum": f"{ad}: {label}{'+' if sign < 0 else '-'}1", ad: _out(b - sign * step, tip),
                              "beklenen": "başarılı"}]
            elif tip == "tarih":
                for label, bound, sign in (("en erken", lo, -1), ("en geç", hi, 1)):
                    off = _gun(bound)
                    if off is None:
                        continue
                    rows += [{"durum": f"{ad}: {label} - 1 gün dışarı", ad: _gun_ifadesi(off + sign),
                              "beklenen": disari},
                             {"durum": f"{ad}: {label}", ad: _gun_ifadesi(off), "beklenen": "başarılı"}]
            elif tip == "metin":
                for label, bound, sign in (("en kısa", lo, -1), ("en uzun", hi, 1)):
                    n = _sayi(bound)
                    if n is None:
                        continue
                    n = int(n)
                    if n + sign >= 0:
                        rows.append({"durum": f"{ad}: {label} {n + sign} karakter (sınır dışı)",
                                     ad: "a" * (n + sign), "beklenen": disari})
                    rows.append({"durum": f"{ad}: {label} {n} karakter", ad: "a" * n, "beklenen": "başarılı"})
        aralik = ik.veri.get("tarih_araligi")
        if aralik:
            b, e = aralik["baslangic"], aralik["bitis"]
            rows += [{"durum": "aynı gün (0 gün)", b: "bugün", e: "bugün", "beklenen": "başarılı"},
                     {"durum": "bitiş başlangıçtan 1 gün önce", b: "bugün", e: "bugün-1", "beklenen": disari}]
            n = aralik.get("en_fazla_gun")
            if n is not None:
                rows += [{"durum": f"tam {n} gün", b: _gun_ifadesi(-n), e: "bugün", "beklenen": "başarılı"},
                         {"durum": f"{n + 1} gün (sınır dışı)", b: _gun_ifadesi(-n - 1), e: "bugün",
                          "beklenen": disari}]
            else:
                notlar.append("tarih aralığının en fazla kaç gün olabileceği tanımlı değil "
                              "(tarih_araligi.en_fazla_gun)")
            rows.append({"durum": "gelecek tarihli bitiş", b: "bugün", e: "bugün+1", "beklenen": "?"})
        if not rows:
            notlar.append(f"'{ik.ad}' girdilerinde sınır yok: en_az / en_fazla veya tarih_araligi ekleyin "
                          "(ya da sınırı keşif çağrılarıyla bulup önerin)")
        return rows

    def _esdeger(self, ik: Kayit, yk: Kayit, notlar: list[str]) -> list[dict[str, Any]]:
        disari = yk.veri.get("beklenen_disari", "hata")
        rows: list[dict[str, Any]] = []
        aralik = ik.veri.get("tarih_araligi") or {}
        aralikta = {aralik.get("baslangic"), aralik.get("bitis")}
        for ad, g in (ik.veri.get("girdiler") or {}).items():
            tip = g.get("tip") or "metin"
            if tip == "tarih" and ad in aralikta:
                continue                     # classified together, as a range (below)
            if tip == "liste":
                rows += [{"durum": f"{ad} = {o}", ad: o, "beklenen": "başarılı"} for o in g.get("secenekler") or []]
                rows.append({"durum": f"{ad}: listede olmayan değer", ad: {"ham": "GECERSIZ"}, "beklenen": disari})
            elif tip == "evet_hayir":
                rows += [{"durum": f"{ad} = evet", ad: "evet", "beklenen": "başarılı"},
                         {"durum": f"{ad} = hayır", ad: "hayır", "beklenen": "başarılı"}]
            elif tip in ("sayi", "ondalik"):
                lo, hi = _sayi(g.get("en_az")), _sayi(g.get("en_fazla"))
                if lo is not None and hi is not None:
                    rows.append({"durum": f"{ad}: aralık içi", ad: _out((lo + hi) / 2 if tip == "ondalik"
                                                                         else (lo + hi) // 2, tip),
                                 "beklenen": "başarılı"})
                elif lo is not None:
                    rows.append({"durum": f"{ad}: aralık içi", ad: _out(lo + 1, tip), "beklenen": "başarılı"})
                if lo is not None:
                    rows.append({"durum": f"{ad}: en azdan küçük", ad: _out(lo - 10, tip), "beklenen": disari})
                if hi is not None:
                    rows.append({"durum": f"{ad}: en fazladan büyük", ad: _out(hi + 10, tip), "beklenen": disari})
                if lo is None and hi is None:
                    rows += [{"durum": f"{ad}: sıfır", ad: 0, "beklenen": "?"},
                             {"durum": f"{ad}: negatif", ad: -1, "beklenen": "?"}]
            elif tip == "tarih":
                rows += [{"durum": f"{ad}: geçmiş", ad: "bugün-10", "beklenen": "başarılı"},
                         {"durum": f"{ad}: bugün", ad: "bugün", "beklenen": "başarılı"},
                         {"durum": f"{ad}: gelecek", ad: "bugün+10", "beklenen": "?"}]
            else:
                rows.append({"durum": f"{ad}: boş metin", ad: "", "beklenen": disari if g.get("zorunlu") else "?"})
        if aralik:
            b, e = aralik["baslangic"], aralik["bitis"]
            rows += [{"durum": "aralık: son 7 gün", b: "bugün-7", e: "bugün", "beklenen": "başarılı"},
                     {"durum": "aralık: tamamen geçmişte", b: "bugün-60", e: "bugün-30", "beklenen": "başarılı"},
                     {"durum": "aralık: ters (bitiş önce)", b: "bugün", e: "bugün-7", "beklenen": disari},
                     {"durum": "aralık: gelecekte", b: "bugün+1", e: "bugün+10", "beklenen": "?"}]
        if not rows:
            notlar.append(f"'{ik.ad}' işleminin sınıflandırılacak girdisi yok")
        return rows

    def _negatif(self, ik: Kayit, yk: Kayit, notlar: list[str]) -> list[dict[str, Any]]:
        disari = yk.veri.get("beklenen_disari", "hata")
        rows: list[dict[str, Any]] = []
        for ad, g in (ik.veri.get("girdiler") or {}).items():
            if g.get("zorunlu"):
                rows.append({"durum": f"{ad} gönderilmedi", ad: None, "beklenen": disari})
            tip = g.get("tip") or "metin"
            if tip in ("sayi", "ondalik"):
                rows.append({"durum": f"{ad}: sayı yerine metin", ad: {"ham": "abc"}, "beklenen": disari})
            elif tip == "tarih":
                rows.append({"durum": f"{ad}: geçersiz tarih", ad: {"ham": "31.02.2026"}, "beklenen": disari})
                rows.append({"durum": f"{ad}: yanlış biçim", ad: {"ham": "2026-13-45"}, "beklenen": disari})
        if not rows:
            notlar.append(f"'{ik.ad}' işleminin zorunlu veya tipli girdisi yok")
        return rows

    def _pairwise(self, ik: Kayit, yk: Kayit, notlar: list[str]) -> list[dict[str, Any]]:
        params: list[tuple[str, list[Any]]] = []
        for ad, g in (ik.veri.get("girdiler") or {}).items():
            tip = g.get("tip") or "metin"
            if tip == "liste" and g.get("secenekler"):
                params.append((ad, list(g["secenekler"])))
            elif tip == "evet_hayir":
                params.append((ad, ["evet", "hayır"]))
            elif tip in ("sayi", "ondalik") and g.get("en_az") is not None and g.get("en_fazla") is not None:
                lo, hi = _sayi(g["en_az"]), _sayi(g["en_fazla"])
                params.append((ad, sorted({_out(lo, tip), _out(hi, tip)}, key=str)))  # type: ignore[arg-type]
        if len(params) < 2:
            notlar.append("pairwise için değer kümesi belli en az iki girdi gerekir (liste, evet_hayir veya "
                          "en_az+en_fazla olan sayı)")
            return []
        combos = tum_ciftler([vals for _, vals in params])
        names = [ad for ad, _ in params]
        notlar.append(f"{len(params)} girdi, {len(combos)} kombinasyonla tüm ikili eşleşmeler kapsandı "
                      f"(tüm kombinasyonlar: {_carpim([len(v) for _, v in params])})")
        return [{"durum": ", ".join(f"{n}={v}" for n, v in zip(names, combo, strict=True)),
                 **dict(zip(names, combo, strict=True)), "beklenen": "başarılı"} for combo in combos]


def _carpim(sizes: list[int]) -> int:
    out = 1
    for s in sizes:
        out *= s
    return out


def tum_ciftler(values: list[list[Any]]) -> list[tuple[Any, ...]]:
    """Greedy all-pairs: rows so that every value pair of every two parameters appears at least once."""
    n = len(values)
    uncovered = {(i, a, j, b) for i in range(n) for j in range(i + 1, n)
                 for a in range(len(values[i])) for b in range(len(values[j]))}
    rows: list[tuple[int, ...]] = []
    while uncovered:
        i0, a0, j0, b0 = min(uncovered)
        row: list[int | None] = [None] * n
        row[i0], row[j0] = a0, b0
        for k in range(n):
            if row[k] is not None:
                continue
            best, best_score = 0, -1
            for v in range(len(values[k])):
                score = sum(1 for m in range(n) if row[m] is not None and
                            ((min(k, m), v if k < m else row[m], max(k, m), row[m] if k < m else v) in uncovered))
                if score > best_score:
                    best, best_score = v, score
            row[k] = best
        full = tuple(int(x) for x in row)  # type: ignore[arg-type]
        for i in range(n):
            for j in range(i + 1, n):
                uncovered.discard((i, full[i], j, full[j]))
        rows.append(full)
    return [tuple(values[i][r[i]] for i in range(n)) for r in rows]


def _tekil(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in rows:
        key = repr(sorted((k, repr(v)) for k, v in row.items() if k != "durum"))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out
