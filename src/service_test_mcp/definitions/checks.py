"""The check language: short lines that the server evaluates deterministically.

A check is one line::

    success                                       the system's business success rule passed
    failure                                       ... failed (an error was expected)
    duration < 3 s                                response time (also: ms)
    error code = INVALID_AMOUNT                   one of the system's error fields equals / contains
    count AccountGroups.Accounts >= 1             how many values the path has
    Value.CustomerName is not empty
    AccountGroups.Accounts.IBANNumber matches ^TR\\d{24}$
    Success = true
    for each FXRateModelList: CurrencyBid <= CurrencyAsk    a field compared with a field of the same item
    for each AccountTransactionHistoryList: TransactionDate between {{start}} and {{end}}

Paths are field names joined with dots; lists are walked automatically (``AccountGroups.Accounts.IBANNumber`` gives
every IBAN of every group). ``[0]`` picks one item. Without ``for each`` a check must hold for every value of the path.

Operators: = != > >= < <= contains, does not contain, matches, is not empty, is empty, exists, missing,
between (x and y). Numbers are compared as numbers and dates as dates (today-7, 28.10.2026, 2026-10-28 ...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..scenario.asserts import tr_fold
from .values import FALSE_WORDS, TRUE_WORDS, parse_date

OPERATORS = ("is not empty", "does not contain", "not contains", "contains", "matches", "between",
             ">=", "<=", "!=", "=", ">", "<", "is empty", "does not exist", "exists", "missing")
_CANON = {"not contains": "does not contain", "does not exist": "missing"}
_UNARY = ("is not empty", "is empty", "exists", "missing")
_MISSING = object()
_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_PATH = re.compile(r"[\w.\[\]]+")
EXAMPLES = ("'success', 'duration < 3 s', 'count X >= 1', 'X is not empty', 'X = Y', 'X matches ^TR', "
            "'for each LIST: A <= B', 'X between a and b'")


class CheckError(ValueError):
    pass


@dataclass
class Check:
    text: str
    kind: str                    # success | duration | error_code | value
    path: str | None = None
    op: str | None = None
    right: str | None = None
    count: bool = False          # "count <path>"
    each: str | None = None      # list path of "for each <list>: ..."
    expect_success: bool | None = None


def resolve(body: Any, path: str) -> list[Any]:
    """Dotted path -> every matching value. Lists are flattened on the way; [n] picks an item."""
    if path in ("", "$", "."):
        return [body]
    out: list[Any] = []
    for value in reach(body, path):  # a path that ends on a list means its items
        out.extend(value if isinstance(value, list) else [value])
    return out


def reach(body: Any, path: str) -> list[Any]:
    """Values the path reaches, without opening the last list (an empty list still counts as reached)."""
    current = [body]
    for name, index in _TOKEN.findall(path.strip()):
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


def _count(body: Any, path: str) -> int:
    return sum(len(v) if isinstance(v, list) else 1 for v in reach(body, path) if v is not None)


def parse(line: str) -> Check:
    text = str(line).strip()
    low = text.lower()
    if low in ("success", "succeeds"):
        return Check(text, "success", expect_success=True)
    if low in ("failure", "fails", "error"):
        return Check(text, "success", expect_success=False)
    m = re.match(r"^(?:duration|time)\s*(<=|<|>=|>)\s*(\d+(?:[.,]\d+)?)\s*(s|sec|seconds|ms)?$", low)
    if m:
        limit = Decimal(m.group(2).replace(",", "."))
        if m.group(3) in ("s", "sec", "seconds") or m.group(3) is None and limit < 100:
            limit *= 1000
        return Check(text, "duration", op=m.group(1), right=str(limit))
    each = None
    bad_path = None
    m = re.match(r"^for\s+each\s+([^:]+):\s*(.+)$", text, re.IGNORECASE)
    if m:
        each, text = m.group(1).strip(), m.group(2).strip()
        low = text.lower()
    m = re.match(r"^error\s+code\s+(=|contains)\s+(.+)$", low)
    if m and not each:
        return Check(text, "error_code", op=m.group(1), right=text[m.start(2):].strip())
    for op in OPERATORS:
        pattern = rf"^(.+?)\s+{re.escape(op)}(?:\s+(.+))?$" if op[0].isalpha() else rf"^(.+?)\s*{re.escape(op)}\s*(.*)$"
        m = re.match(pattern, text, re.IGNORECASE)
        if not m:
            continue
        canon = _CANON.get(op, op)
        left, right = m.group(1).strip(), (m.group(2) or "").strip()
        if canon in _UNARY and right:
            continue
        if canon not in _UNARY and not right:
            raise CheckError(f"'{line}': '{canon}' needs a value")
        if canon == "between" and not re.search(r"\s+and\s+", right):
            raise CheckError(f"'{line}': write 'between' as: <field> between <a> and <b>")
        if canon == "matches":
            try:
                re.compile(right)
            except re.error as exc:
                raise CheckError(f"'{line}': invalid pattern: {exc}") from None
        count = False
        m2 = re.match(r"^count\s+(?:of\s+)?(.+)$", left, re.IGNORECASE)
        if m2:
            left, count = m2.group(1).strip(), True
        if not _PATH.fullmatch(left):
            bad_path = left
            continue
        return Check(str(line).strip(), "value", path=left, op=canon, right=right or None, count=count, each=each)
    if bad_path:
        raise CheckError(f"'{line}': '{bad_path}' is not a field path (e.g. AccountGroups.Accounts.IBANNumber)")
    raise CheckError(f"'{line}' is not a check. Examples: {EXAMPLES}")


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
    if t.lower() in ("true", "yes"):
        return True
    if t.lower() in ("false", "no"):
        return False
    if t.lower() in ("null", "none", "empty"):
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
                ld, rd = parse_date(left), parse_date(right)
                same = ld == rd if ld and rd else str(left) == str(right)
        return same if op == "=" else not same
    ln, rn = _number(left), _number(right)
    if ln is None or rn is None:
        ld, rd = parse_date(left), parse_date(right)
        if ld is None or rd is None:
            raise CheckError(f"cannot compare: {left!r} {op} {right!r}")
        ln, rn = ld.timestamp(), rd.timestamp()  # type: ignore[assignment]
    return {">": ln > rn, ">=": ln >= rn, "<": ln < rn, "<=": ln <= rn}[op]  # type: ignore[operator]


def _apply(op: str, value: Any, right: Any) -> bool:
    if op == "is not empty":
        return value not in (None, "", [], {}) and not (isinstance(value, str) and not value.strip())
    if op == "is empty":
        return value in (None, "", [], {}) or (isinstance(value, str) and not value.strip())
    if op in ("contains", "does not contain"):
        found = tr_fold(right) in tr_fold(value) if not isinstance(value, list) else \
            any(tr_fold(right) == tr_fold(v) for v in value)
        return found if op == "contains" else not found
    if op == "matches":
        return re.search(str(right), str(value)) is not None
    if op == "between":
        a, b = re.split(r"\s+and\s+", str(right), maxsplit=1) if isinstance(right, str) else right
        return _compare(value, ">=", _literal(a)) and _compare(value, "<=", _literal(b))
    return _compare(value, op, right)


def evaluate(c: Check, result: dict[str, Any], error_paths: list[str]) -> dict[str, Any]:
    """Evaluate one parsed check against a call result (body, status, elapsed_ms, business)."""
    out: dict[str, Any] = {"check": c.text}
    body = result.get("body")
    business = result.get("business") or {}
    try:
        if c.kind == "success":
            errors = business.get("errors")
            out.update(passed=bool(business.get("success")) is c.expect_success,
                       actual="success" if business.get("success") else
                       f"failure{': ' + '; '.join(map(str, errors)) if errors else ''}")
        elif c.kind == "duration":
            took = result.get("elapsed_ms")
            out.update(passed=took is not None and _compare(took, c.op or "<", Decimal(c.right or "0")),
                       actual=f"{took} ms")
        elif c.kind == "error_code":
            values = [v for p in error_paths for v in resolve(body, p)]
            want = _literal(c.right or "")
            ok = any((str(v) == str(want)) if c.op == "=" else (str(want).lower() in str(v).lower()) for v in values)
            out.update(passed=ok, actual=values[:5])
        elif c.each:
            items = resolve(body, c.each)
            if not items:
                out.update(passed=False, actual=None, message=f"list '{c.each}' is empty or missing")
                return out
            fails = []
            for i, item in enumerate(items):
                left = reach(item, c.path or "")
                lv = left[0] if left else _MISSING
                right: Any = None
                if c.right is not None:
                    side = resolve(item, c.right) if _PATH.fullmatch(c.right) else []
                    right = side[0] if side else _literal(c.right)
                if lv is _MISSING:
                    ok = c.op == "missing"
                elif c.op in ("exists", "missing"):
                    ok = c.op == "exists"
                else:
                    ok = _apply(c.op or "=", lv, right)
                if not ok:
                    fails.append({"index": i, "value": None if lv is _MISSING else lv, "other": right})
            out.update(passed=not fails, actual=f"{len(items)} items" if not fails else fails[:5],
                       message="ok" if not fails else f"{len(fails)}/{len(items)} items do not match")
        else:
            if c.count:
                n = _count(body, c.path or "")
                out.update(passed=_apply(c.op or ">=", n, _literal(c.right or "0")), actual=n)
                return out
            if c.op in ("exists", "missing"):
                reached = [v for v in reach(body, c.path or "") if v is not None]
                out.update(passed=bool(reached) if c.op == "exists" else not reached,
                           actual="exists" if reached else "missing")
                return out
            values = resolve(body, c.path or "")
            if not values:
                ok = c.op == "is empty"
                out.update(passed=ok, actual=None, message="ok" if ok else f"'{c.path}' is not in the response")
                return out
            right = _literal(c.right) if c.right is not None else None
            bad = [v for v in values if not _apply(c.op or "=", v, right)]
            out.update(passed=not bad, actual=values[0] if len(values) == 1 else
                       (f"{len(values)} values" if not bad else bad[:5]))
    except CheckError as exc:
        out.update(passed=False, message=str(exc))
    return out


def run_checks(lines: list[str], result: dict[str, Any], error_paths: list[str]) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        try:
            out.append(evaluate(parse(line), result, error_paths))
        except CheckError as exc:
            out.append({"check": line, "passed": False, "message": str(exc)})
    return out
