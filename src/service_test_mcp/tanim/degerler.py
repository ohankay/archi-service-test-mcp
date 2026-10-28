"""Values written by people in definitions: date expressions, date formats, input types.

* ``bugün``, ``bugün-7``, ``bugün+1`` (also ``bugun``, ``today``): a date relative to today.
* Date formats use Turkish letters: ``GG.AA.YYYY`` (28.10.2026), ``YYYY-AA-GG`` (2026-10-28), ``GG/AA/YYYY``.
* Input types: ``metin``, ``sayi``, ``ondalik``, ``tarih``, ``evet_hayir``, ``liste``.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

TIPLER = ("metin", "sayi", "ondalik", "tarih", "evet_hayir", "liste")
_GUN = re.compile(r"^\s*(bugün|bugun|today)\s*(?:([+-])\s*(\d+))?\s*$", re.IGNORECASE)
_PARSE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                  "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")
TRUE_WORDS = ("evet", "true", "doğru", "dogru", "1", "yes")
FALSE_WORDS = ("hayır", "hayir", "false", "yanlış", "yanlis", "0", "no")


class DegerHatasi(ValueError):
    pass


def bicim_to_strftime(bicim: str | None) -> str:
    if not bicim:
        return "%Y-%m-%d"
    out = bicim.upper().replace("YYYY", "%Y").replace("GG", "%d").replace("AA", "%m")
    out = out.replace("SS", "%H").replace("DD", "%M")
    return out


def gun_ifadesi(value: Any) -> _dt.date | None:
    """``bugün-7`` -> date, or None when the value is not a day expression."""
    if not isinstance(value, str):
        return None
    m = _GUN.match(value)
    if not m:
        return None
    days = int(m.group(3) or 0) * (-1 if m.group(2) == "-" else 1)
    return _dt.date.today() + _dt.timedelta(days=days)


def tarih_coz(value: Any) -> _dt.datetime | None:
    """Any date-like value (expression, common formats, .NET /Date(ms)/) -> datetime, else None."""
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    day = gun_ifadesi(value)
    if day:
        return _dt.datetime(day.year, day.month, day.day)
    text = value.strip()
    m = re.match(r"^/Date\((-?\d+)([+-]\d{4})?\)/$", text)
    if m:
        return _dt.datetime.fromtimestamp(int(m.group(1)) / 1000)
    text = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", text)
    for fmt in _PARSE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def donustur(value: Any, tip: str, bicim: str | None = None) -> Any:
    """A value written in a definition / by the agent -> the value sent to the service."""
    if value is None:
        return None
    if tip == "tarih":
        when = tarih_coz(value)
        if when is None:
            raise DegerHatasi(f"tarih anlaşılamadı: {value!r} (ör. bugün-7 veya {bicim or 'GG.AA.YYYY'})")
        return when.strftime(bicim_to_strftime(bicim))
    if tip == "sayi":
        if isinstance(value, bool):
            raise DegerHatasi(f"sayı bekleniyordu: {value!r}")
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except ValueError:
            raise DegerHatasi(f"sayı bekleniyordu: {value!r}") from None
    if tip == "ondalik":
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return value
        try:
            return Decimal(str(value).strip().replace(",", "."))
        except InvalidOperation:
            raise DegerHatasi(f"ondalık sayı bekleniyordu: {value!r}") from None
    if tip == "evet_hayir":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE_WORDS:
            return True
        if text in FALSE_WORDS:
            return False
        raise DegerHatasi(f"evet/hayır bekleniyordu: {value!r}")
    return value


def ham_gonder(value: Any) -> Any:
    """Negative tests may send a deliberately wrong value: ``{ham: ...}`` bypasses the type conversion."""
    return value["ham"] if isinstance(value, dict) and set(value) == {"ham"} else value
