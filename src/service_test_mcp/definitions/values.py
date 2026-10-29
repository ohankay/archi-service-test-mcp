"""Values written by people in definitions: date expressions, date formats, input types.

* ``today``, ``today-7``, ``today+1``: a date relative to today.
* Date formats: ``DD.MM.YYYY`` (28.10.2026), ``YYYY-MM-DD`` (2026-10-28), ``DD/MM/YYYY``; time ``HH:mm:ss``.
* Input types: ``text``, ``integer``, ``decimal``, ``date``, ``boolean``, ``list``.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

TYPES = ("text", "integer", "decimal", "date", "boolean", "list")
_DAY = re.compile(r"^\s*today\s*(?:([+-])\s*(\d+))?\s*$", re.IGNORECASE)
_PARSE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                  "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")
TRUE_WORDS = ("true", "yes", "1")
FALSE_WORDS = ("false", "no", "0")


class ValueProblem(ValueError):
    pass


def format_to_strftime(fmt: str | None) -> str:
    """'DD.MM.YYYY HH:mm:ss' -> '%d.%m.%Y %H:%M:%S' (MM is the month, mm the minute)."""
    if not fmt:
        return "%Y-%m-%d"
    out = fmt.replace("YYYY", "%Y").replace("DD", "%d").replace("MM", "%m")
    return out.replace("HH", "%H").replace("mm", "%M").replace("ss", "%S")


def day_expression(value: Any) -> _dt.date | None:
    """``today-7`` -> date, or None when the value is not a day expression."""
    if not isinstance(value, str):
        return None
    m = _DAY.match(value)
    if not m:
        return None
    days = int(m.group(2) or 0) * (-1 if m.group(1) == "-" else 1)
    return _dt.date.today() + _dt.timedelta(days=days)


def day_text(offset: int) -> str:
    """-30 -> 'today-30'."""
    return "today" if offset == 0 else f"today{offset:+d}"


def parse_date(value: Any) -> _dt.datetime | None:
    """Any date-like value (expression, common formats, .NET /Date(ms)/) -> datetime, else None."""
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    day = day_expression(value)
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


def convert(value: Any, kind: str, fmt: str | None = None) -> Any:
    """A value written in a definition / by the agent -> the value sent to the service."""
    if value is None:
        return None
    if kind == "date":
        when = parse_date(value)
        if when is None:
            raise ValueProblem(f"not a date: {value!r} (e.g. today-7 or {fmt or 'DD.MM.YYYY'})")
        return when.strftime(format_to_strftime(fmt))
    if kind == "integer":
        if isinstance(value, bool):
            raise ValueProblem(f"an integer was expected: {value!r}")
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except ValueError:
            raise ValueProblem(f"an integer was expected: {value!r}") from None
    if kind == "decimal":
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return value
        try:
            return Decimal(str(value).strip().replace(",", "."))
        except InvalidOperation:
            raise ValueProblem(f"a decimal number was expected: {value!r}") from None
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE_WORDS:
            return True
        if text in FALSE_WORDS:
            return False
        raise ValueProblem(f"true/false was expected: {value!r}")
    return value


def raw_value(value: Any) -> tuple[bool, Any]:
    """Negative tests may send a deliberately wrong value: ``{raw: ...}`` bypasses the type conversion."""
    if isinstance(value, dict) and set(value) == {"raw"}:
        return True, value["raw"]
    return False, value
