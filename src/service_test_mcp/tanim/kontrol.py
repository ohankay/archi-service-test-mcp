"""The check language: short Turkish lines that the server evaluates deterministically.

A check is one line::

    başarılı                                     the system's business success rule passed
    başarısız                                    ... failed (an error was expected)
    süre < 3 sn                                  response time (also: ms)
    hata kodu = INVALID_AMOUNT                   one of the system's error fields equals / contains
    AccountGroups.Accounts sayısı >= 1           how many values the path has
    Value.CustomerName boş değil
    AccountGroups.Accounts.IBANNumber eşleşir ^TR\\d{24}$
    Success = true
    her FXRateModelList için CurrencyBid <= CurrencyAsk    a field compared with a field of the same item
    her AccountTransactionHistoryList için TransactionDate arasında {{baslangic}} ve {{bitis}}

Paths are field names joined with dots; lists are walked automatically (``AccountGroups.Accounts.IBANNumber`` gives
every IBAN of every group). ``[0]`` picks one item. Without ``her`` a check must hold for every value of the path.

Operators: = != > >= < <= içerir içermez eşleşir "boş değil" boş var yok arasında (x ve y).
Numbers are compared as numbers and dates as dates (bugün-7, 28.10.2026, 2026-10-28 ...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..scenario.asserts import tr_fold
from .degerler import FALSE_WORDS, TRUE_WORDS, tarih_coz

OPERATORLER = ("boş değil", "bos degil", "içermez", "icermez", "içerir", "icerir", "eşleşir", "eslesir",
               "arasında", "arasinda", ">=", "<=", "!=", "=", ">", "<", "boş", "bos", "var", "yok")
_CANON = {"bos degil": "boş değil", "icermez": "içermez", "icerir": "içerir", "esler": "eşleşir",
          "eslesir": "eşleşir", "arasinda": "arasında", "bos": "boş"}
_UNARY = ("boş değil", "boş", "var", "yok")
_MISSING = object()
_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class KontrolHatasi(ValueError):
    pass


@dataclass
class Kontrol:
    metin: str
    tur: str                 # basari | sure | hata_kodu | deger
    yol: str | None = None
    op: str | None = None
    sag: str | None = None
    say: bool = False        # "<yol> sayısı"
    her: str | None = None   # list path for "her <liste>: ..."
    beklenen_basari: bool | None = None


def coz(body: Any, yol: str) -> list[Any]:
    """Dotted path -> every matching value. Lists are flattened on the way; [n] picks an item."""
    if yol in ("", "$", "."):
        return [body]
    current = [body]
    for name, index in _TOKEN.findall(yol.strip()):
        nxt: list[Any] = []
        for value in current:
            if index:
                if isinstance(value, list) and int(index) < len(value):
                    nxt.append(value[int(index)])
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, dict) and name in item:
                    nxt.append(item[name])
        current = nxt
    out: list[Any] = []
    for value in current:  # a path that ends on a list means its items
        out.extend(value if isinstance(value, list) else [value])
    return out


def _ulas(body: Any, yol: str) -> list[Any]:
    """Values the path reaches, without opening the last list (an empty list still counts as reached)."""
    current = [body]
    for name, index in _TOKEN.findall(yol.strip()):
        nxt: list[Any] = []
        for value in current:
            if index:
                if isinstance(value, list) and int(index) < len(value):
                    nxt.append(value[int(index)])
                continue
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, dict) and name in item:
                    nxt.append(item[name])
        current = nxt
    return current


def _count(body: Any, yol: str) -> int:
    return sum(len(v) if isinstance(v, list) else 1 for v in _ulas(body, yol) if v is not None)


def ayristir(metin: str) -> Kontrol:
    text = str(metin).strip()
    low = text.lower()
    if low in ("başarılı", "basarili", "başarılı olmalı"):
        return Kontrol(text, "basari", beklenen_basari=True)
    if low in ("başarısız", "basarisiz", "hata", "hata vermeli"):
        return Kontrol(text, "basari", beklenen_basari=False)
    m = re.match(r"^s[üu]re\s*(<=|<|>=|>)\s*(\d+(?:[.,]\d+)?)\s*(sn|saniye|ms)?$", low)
    if m:
        sure = Decimal(m.group(2).replace(",", "."))
        if m.group(3) in ("sn", "saniye") or m.group(3) is None and sure < 100:
            sure *= 1000
        return Kontrol(text, "sure", op=m.group(1), sag=str(sure))
    her = None
    bad_path = None
    m = re.match(r"^her\s+(\S+)\s+i[çc]in\s+(.+)$", text, re.IGNORECASE) or \
        re.match(r"^her\s+([^:]+):\s*(.+)$", text, re.IGNORECASE)
    if m:
        her, text = m.group(1).strip(), m.group(2).strip()
        low = text.lower()
    m = re.match(r"^hata\s+kodu\s+(=|içerir|icerir)\s+(.+)$", low)
    if m and not her:
        return Kontrol(text, "hata_kodu", op=_CANON.get(m.group(1), m.group(1)), sag=text[m.start(2):].strip())
    for op in OPERATORLER:
        pattern = rf"^(.+?)\s+{re.escape(op)}(?:\s+(.+))?$" if op[0].isalpha() else rf"^(.+?)\s*{re.escape(op)}\s*(.*)$"
        m = re.match(pattern, text, re.IGNORECASE)
        if not m:
            continue
        canon = _CANON.get(op, op)
        sol, sag = m.group(1).strip(), (m.group(2) or "").strip()
        if canon in _UNARY and sag:
            continue
        if canon not in _UNARY and not sag:
            raise KontrolHatasi(f"'{metin}': '{canon}' için bir değer gerekli")
        if canon == "arasında" and not re.search(r"\s+ve\s+", sag):
            raise KontrolHatasi(f"'{metin}': 'arasında' şöyle yazılır: <alan> arasında <a> ve <b>")
        if canon == "eşleşir":
            try:
                re.compile(sag)
            except re.error as exc:
                raise KontrolHatasi(f"'{metin}': geçersiz kalıp: {exc}") from None
        say = False
        if re.search(r"\s+say[ıi]s[ıi]$", sol, re.IGNORECASE):
            sol, say = re.sub(r"\s+say[ıi]s[ıi]$", "", sol, flags=re.IGNORECASE), True
        if not re.fullmatch(r"[\w.\[\]]+", sol):
            bad_path = sol
            continue
        return Kontrol(str(metin).strip(), "deger", yol=sol, op=canon, sag=sag or None, say=say, her=her)
    if bad_path:
        raise KontrolHatasi(f"'{metin}': '{bad_path}' bir alan yolu değil (ör. AccountGroups.Accounts.IBANNumber)")
    raise KontrolHatasi(f"'{metin}' anlaşılamadı. Örnekler: 'başarılı', 'süre < 3 sn', 'X sayısı >= 1', "
                        "'X boş değil', 'X = Y', 'X eşleşir ^TR', 'her Liste için A <= B', 'X arasında a ve b'")


# -- evaluation ---------------------------------------------------------------------------------------------------
def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).strip().replace(",", ".")) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        return None


def _literal(text: str) -> Any:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    if t.lower() in ("true", "evet"):
        return True
    if t.lower() in ("false", "hayır", "hayir"):
        return False
    if t.lower() in ("null", "boş", "bos", "none"):
        return None
    return t


def _compare(left: Any, op: str, right: Any) -> bool:
    if op in ("=", "!="):
        if isinstance(right, bool):
            text = str(left).lower()
            lb = left if isinstance(left, bool) else (True if text in TRUE_WORDS else False if text in FALSE_WORDS
                                                      else None)
            same = lb is right
        elif right is None or left is None:
            same = left is None and right is None
        else:
            ln, rn = _number(left), _number(right)
            if ln is not None and rn is not None:
                same = ln == rn
            else:
                ld, rd = tarih_coz(left), tarih_coz(right)
                same = ld == rd if ld and rd else str(left) == str(right)
        return same if op == "=" else not same
    ln, rn = _number(left), _number(right)
    if ln is None or rn is None:
        ld, rd = tarih_coz(left), tarih_coz(right)
        if ld is None or rd is None:
            raise KontrolHatasi(f"karşılaştırılamaz: {left!r} {op} {right!r}")
        ln, rn = ld.timestamp(), rd.timestamp()  # type: ignore[assignment]
    return {">": ln > rn, ">=": ln >= rn, "<": ln < rn, "<=": ln <= rn}[op]  # type: ignore[operator]


def _apply(op: str, value: Any, sag: Any) -> bool:
    if op == "boş değil":
        return value not in (None, "", [], {}) and not (isinstance(value, str) and not value.strip())
    if op == "boş":
        return value in (None, "", [], {}) or (isinstance(value, str) and not value.strip())
    if op in ("içerir", "içermez"):
        found = tr_fold(sag) in tr_fold(value) if not isinstance(value, list) else \
            any(tr_fold(sag) == tr_fold(v) for v in value)
        return found if op == "içerir" else not found
    if op == "eşleşir":
        return re.search(str(sag), str(value)) is not None
    if op == "arasında":
        a, b = re.split(r"\s+ve\s+", str(sag), maxsplit=1) if isinstance(sag, str) else sag
        return _compare(value, ">=", _literal(a)) and _compare(value, "<=", _literal(b))
    return _compare(value, op, sag)


def degerlendir(k: Kontrol, sonuc: dict[str, Any], hata_yollari: list[str]) -> dict[str, Any]:
    """Evaluate one parsed check against a call result (body, status, elapsed_ms, business)."""
    out: dict[str, Any] = {"kontrol": k.metin}
    body = sonuc.get("body")
    try:
        if k.tur == "basari":
            ok = bool((sonuc.get("business") or {}).get("success")) is k.beklenen_basari
            hatalar = (sonuc.get("business") or {}).get("errors")
            out.update(gecti=ok, gercek="başarılı" if (sonuc.get("business") or {}).get("success") else
                       f"başarısız{': ' + '; '.join(map(str, hatalar)) if hatalar else ''}")
        elif k.tur == "sure":
            sure = sonuc.get("elapsed_ms")
            ok = sure is not None and _compare(sure, k.op or "<", Decimal(k.sag or "0"))
            out.update(gecti=ok, gercek=f"{sure} ms")
        elif k.tur == "hata_kodu":
            values = [v for p in hata_yollari for v in coz(body, p)]
            want = _literal(k.sag or "")
            ok = any((str(v) == str(want)) if k.op == "=" else (str(want).lower() in str(v).lower()) for v in values)
            out.update(gecti=ok, gercek=values[:5])
        elif k.her:
            items = coz(body, k.her)
            if not items:
                out.update(gecti=False, gercek=None, mesaj=f"'{k.her}' listesi boş veya yok")
                return out
            fails = []
            for i, item in enumerate(items):
                left = _ulas(item, k.yol or "")
                lv = left[0] if left else _MISSING
                sag: Any = None
                if k.sag is not None:
                    side = coz(item, k.sag) if re.fullmatch(r"[\w.\[\]]+", k.sag) else []
                    sag = side[0] if side else _literal(k.sag)
                if lv is _MISSING:
                    ok = k.op == "yok"
                elif k.op in ("var", "yok"):
                    ok = k.op == "var"
                else:
                    ok = _apply(k.op or "=", lv, sag)
                if not ok:
                    fails.append({"sira": i, "deger": None if lv is _MISSING else lv, "karsi": sag})
            out.update(gecti=not fails, gercek=f"{len(items)} kayıt" if not fails else fails[:5],
                       mesaj="ok" if not fails else f"{len(fails)}/{len(items)} kayıt uymadı")
        else:
            if k.say:
                n = _count(body, k.yol or "")
                ok = _apply(k.op or ">=", n, _literal(k.sag or "0"))
                out.update(gecti=ok, gercek=n)
                return out
            if k.op in ("var", "yok"):
                reached = [v for v in _ulas(body, k.yol or "") if v is not None]
                ok = bool(reached) if k.op == "var" else not reached
                out.update(gecti=ok, gercek="var" if reached else "yok")
                return out
            values = coz(body, k.yol or "")
            if not values:
                ok = k.op == "boş"
                out.update(gecti=ok, gercek=None, mesaj="ok" if ok else f"'{k.yol}' yanıtta yok")
                return out
            sag = _literal(k.sag) if k.sag is not None else None
            bad = [v for v in values if not _apply(k.op or "=", v, sag)]
            out.update(gecti=not bad, gercek=values[0] if len(values) == 1 else
                       (f"{len(values)} değer" if not bad else bad[:5]))
    except KontrolHatasi as exc:
        out.update(gecti=False, mesaj=str(exc))
    return out


def kontrol_et(kontroller: list[str], sonuc: dict[str, Any], hata_yollari: list[str]) -> list[dict[str, Any]]:
    out = []
    for metin in kontroller:
        try:
            out.append(degerlendir(ayristir(metin), sonuc, hata_yollari))
        except KontrolHatasi as exc:
            out.append({"kontrol": metin, "gecti": False, "mesaj": str(exc)})
    return out
