"""Assertions - evaluated inside the MCP server, deterministically, on the real (unmasked) response.

Step-level checks::

    - fault: none                 # SOAP: no fault   ("any" = a fault is expected)
    - fault: { code: Client, message_contains: "bulunamadı" }
    - status: 200                 # or [200, 201] or { gte: 200, lt: 300 }
    - responseTimeMs: { lt: 2000 }
    - header: Content-Type
      contains: json
    - schema: schemas/bakiye.json # JSON Schema for the whole body (or with 'path')
    - success: true               # profile services: the profile's business success rule ($.Success, ...)

Value checks on a JSONPath (``$.Sonuc.Bakiye``, ``$.items[*].id``, ``$..Kod``, ``$.items[?(@.durum == 'AKTIF')]``)::

    - path: $.Sonuc.Basarili
      equals: true
    operators: equals, notEquals, equalsIgnoreCase, contains, notContains, startsWith, endsWith, matches, in, notIn,
               exists, notEmpty, isNull, type, numeric, count, length, schema
    options:   all: true  (apply to every match)   message: "text shown on failure"
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MISSING = object()
VALUE_OPERATORS = ("equals", "notEquals", "equalsIgnoreCase", "contains", "notContains", "startsWith", "endsWith",
                   "matches", "in", "notIn", "exists", "notEmpty", "isNull", "type", "numeric", "count", "length",
                   "schema")
NUMERIC_KEYS = ("eq", "ne", "gt", "gte", "lt", "lte", "between", "tolerance")
TYPES = ("string", "number", "integer", "decimal", "boolean", "object", "array", "null", "date", "datetime")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_ALIASES = {"response_time_ms": "responseTimeMs", "responsetimems": "responseTimeMs", "not_equals": "notEquals",
            "not_contains": "notContains", "not_in": "notIn", "not_empty": "notEmpty", "is_null": "isNull",
            "starts_with": "startsWith", "ends_with": "endsWith", "equals_ignore_case": "equalsIgnoreCase",
            "eq": "equals", "ne": "notEquals"}


class AssertionSpecError(ValueError):
    pass


def tr_fold(text: Any) -> str:
    """Case-insensitive form that respects Turkish I/ı and İ/i ("HATALI" == "hatalı")."""
    return str(text).replace("I", "ı").replace("İ", "i").lower()


def _canon(spec: dict[str, Any]) -> dict[str, Any]:
    return {_ALIASES.get(str(k), _ALIASES.get(str(k).lower(), str(k))): v for k, v in spec.items()}


def validate_assertion(spec: Any) -> list[str]:
    """Returns a list of problems (empty = valid)."""
    if not isinstance(spec, dict) or not spec:
        return [f"assertion must be a non-empty mapping, got {spec!r}"]
    spec = _canon(spec)
    keys = set(spec) - {"message", "all"}
    errors: list[str] = []
    if "path" in spec:
        ops = keys - {"path"}
        if len(ops) != 1 or not ops <= set(VALUE_OPERATORS):
            return [f"'path' assertion needs exactly one operator from {', '.join(VALUE_OPERATORS)}; got "
                    f"{', '.join(sorted(ops)) or 'none'}"]
        op = ops.pop()
        try:
            from jsonpath_ng.ext import parse

            parse(str(spec["path"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"invalid JSONPath {spec['path']!r}: {exc}")
        errors += _check_operand(op, spec[op])
        return errors
    if "header" in spec:
        ops = keys - {"header"}
        if len(ops) != 1 or not ops <= {"equals", "contains", "matches", "exists", "notContains"}:
            return ["'header' assertion needs one of equals, contains, notContains, matches, exists"]
        return errors
    if keys == {"fault"}:
        value = spec["fault"]
        if isinstance(value, dict):
            bad = set(value) - {"code", "message_contains", "message"}
            if bad:
                errors.append(f"fault: unknown keys {', '.join(sorted(bad))} (code, message_contains)")
        elif not isinstance(value, str) and value is not None and value is not False:
            errors.append("fault must be 'none', 'any', a code string or {code, message_contains}")
        return errors
    if keys == {"status"}:
        return _check_operand("status", spec["status"])
    if keys == {"responseTimeMs"}:
        return _check_operand("numeric", spec["responseTimeMs"])
    if keys == {"schema"}:
        return []
    if keys == {"success"}:
        return [] if isinstance(spec["success"], bool) else ["success must be true or false"]
    return [f"unknown assertion {spec!r}; use one of: fault, status, responseTimeMs, header, schema, success, or "
            f"path + operator ({', '.join(VALUE_OPERATORS)})"]


def _check_operand(op: str, value: Any) -> list[str]:
    if op == "type":
        names = value if isinstance(value, list) else [value]
        bad = [n for n in names if n not in TYPES]
        return [f"type must be one of {', '.join(TYPES)}; got {', '.join(map(str, bad))}"] if bad else []
    if op in ("numeric",) or (op in ("count", "length", "status") and isinstance(value, dict)):
        if not isinstance(value, dict):
            return [f"'{op}' needs a mapping like {{gte: 0, lt: 10}}"]
        bad = set(value) - set(NUMERIC_KEYS)
        return [f"'{op}': unknown keys {', '.join(sorted(bad))}; allowed {', '.join(NUMERIC_KEYS)}"] if bad else []
    if op in ("in", "notIn") and not isinstance(value, list):
        return [f"'{op}' needs a list"]
    if op == "matches":
        try:
            re.compile(str(value))
        except re.error as exc:
            return [f"invalid regex {value!r}: {exc}"]
    return []


# -- evaluation helpers ----------------------------------------------------------------------------------------
def to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if re.fullmatch(r"[+-]?\d+([.,]\d+)?", text):
            try:
                return Decimal(text.replace(",", "."))
            except InvalidOperation:
                return None
    return None


def values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        if isinstance(expected, bool) and isinstance(actual, str):
            return actual.strip().lower() == str(expected).lower()
        if isinstance(actual, bool) and isinstance(expected, str):
            return expected.strip().lower() == str(actual).lower()
        return actual is expected or actual == expected and type(actual) is type(expected)
    if isinstance(expected, (int, float, Decimal)) or isinstance(actual, (int, float, Decimal)):
        a, e = to_decimal(actual), to_decimal(expected)
        if a is not None and e is not None:
            return a == e
    if isinstance(expected, dict) and isinstance(actual, dict):
        return set(expected) == set(actual) and all(values_equal(actual[k], expected[k]) for k in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(values_equal(a, e) for a, e in zip(actual, expected))
    if expected is None or actual is None:
        return expected is None and actual is None
    return actual == expected


def _numeric(actual: Any, spec: dict[str, Any]) -> tuple[bool, str]:
    a = to_decimal(actual)
    if a is None:
        return False, f"value {actual!r} is not numeric"
    tol = to_decimal(spec.get("tolerance", 0)) or Decimal(0)
    checks = []
    for key, bound in spec.items():
        if key == "tolerance":
            continue
        if key == "between":
            lo, hi = (to_decimal(bound[0]), to_decimal(bound[1])) if isinstance(bound, list) and len(bound) == 2 \
                else (None, None)
            if lo is None or hi is None:
                return False, "between needs [low, high]"
            checks.append((lo - tol <= a <= hi + tol, f"between {lo} and {hi}"))
            continue
        b = to_decimal(bound)
        if b is None:
            return False, f"'{key}' bound {bound!r} is not numeric"
        ok = {"eq": abs(a - b) <= tol, "ne": abs(a - b) > tol, "gt": a > b - tol, "gte": a >= b - tol,
              "lt": a < b + tol, "lte": a <= b + tol}[key]
        checks.append((ok, f"{key} {b}"))
    failed = [d for ok, d in checks if not ok]
    return (not failed), ("ok" if not failed else f"{a} is not " + ", ".join(failed))


def _type_ok(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "integer":
        return not isinstance(value, bool) and (isinstance(value, int) or
                                                (isinstance(value, Decimal) and value == value.to_integral_value()))
    if name == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float, Decimal))
    if name == "decimal":
        return to_decimal(value) is not None
    if name == "date":
        return isinstance(value, str) and bool(_DATE.match(value) or _DATETIME.match(value))
    if name == "datetime":
        return isinstance(value, str) and bool(_DATETIME.match(value))
    return False


def _size_check(n: int, spec: Any, what: str) -> tuple[bool, str]:
    if isinstance(spec, dict):
        ok, msg = _numeric(n, spec)
        return ok, f"{what} {msg}" if not ok else "ok"
    return (n == int(spec), "ok" if n == int(spec) else f"{what} is {n}, expected {spec}")


def _json_schema_check(value: Any, schema: Any, base_dir: Path | None) -> tuple[bool, str]:
    import jsonschema

    from ..normalize import json_ready

    if isinstance(schema, str):
        path = Path(schema)
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        if not path.exists():
            return False, f"schema file not found: {path}"
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            schema = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        else:
            schema = json.loads(path.read_text(encoding="utf-8-sig"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(json_ready(value)), key=lambda e: list(e.absolute_path))
    if not errors:
        return True, "ok"
    first = errors[:5]
    return False, "; ".join(f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}" for e in first) + \
        (f" (+{len(errors) - 5} more)" if len(errors) > 5 else "")


def _apply(op: str, actual: Any, expected: Any, base_dir: Path | None, matches: int) -> tuple[bool, str]:
    if op == "exists":
        present = actual is not MISSING
        return (present == bool(expected), "ok" if present == bool(expected) else
                ("path not found" if expected else "path exists but should not"))
    if actual is MISSING:
        if op == "count":
            return _size_check(0, expected, "match count")
        return False, "path not found in response"
    if op == "equals":
        return values_equal(actual, expected), "ok" if values_equal(actual, expected) else "values differ"
    if op == "notEquals":
        return (not values_equal(actual, expected)), "ok" if not values_equal(actual, expected) else "values are equal"
    if op == "equalsIgnoreCase":
        ok = tr_fold(actual) == tr_fold(expected)
        return ok, "ok" if ok else "values differ"
    if op in ("contains", "notContains"):
        if isinstance(actual, list):
            found = any(values_equal(a, expected) for a in actual)
        elif isinstance(actual, dict):
            found = str(expected) in actual
        else:
            found = str(expected) in str(actual)
        ok = found if op == "contains" else not found
        return ok, "ok" if ok else ("not contained" if op == "contains" else "is contained")
    if op == "startsWith":
        ok = str(actual).startswith(str(expected))
        return ok, "ok" if ok else "does not start with expected text"
    if op == "endsWith":
        ok = str(actual).endswith(str(expected))
        return ok, "ok" if ok else "does not end with expected text"
    if op == "matches":
        ok = re.search(str(expected), str(actual)) is not None
        return ok, "ok" if ok else "regex does not match"
    if op in ("in", "notIn"):
        found = any(values_equal(actual, e) for e in expected)
        ok = found if op == "in" else not found
        return ok, "ok" if ok else ("not in list" if op == "in" else "is in list")
    if op == "notEmpty":
        empty = actual is None or (isinstance(actual, (str, list, dict)) and len(actual) == 0) or \
            (isinstance(actual, str) and not actual.strip())
        ok = (not empty) == bool(expected)
        return ok, "ok" if ok else ("value is empty" if expected else "value is not empty")
    if op == "isNull":
        ok = (actual is None) == bool(expected)
        return ok, "ok" if ok else ("value is not null" if expected else "value is null")
    if op == "type":
        names = expected if isinstance(expected, list) else [expected]
        ok = any(_type_ok(actual, n) for n in names)
        return ok, "ok" if ok else f"type is {_describe_type(actual)}"
    if op == "numeric":
        return _numeric(actual, expected)
    if op == "count":
        n = len(actual) if matches == 1 and isinstance(actual, list) else matches
        return _size_check(n, expected, "count")
    if op == "length":
        if not hasattr(actual, "__len__"):
            return False, f"{_describe_type(actual)} has no length"
        return _size_check(len(actual), expected, "length")
    if op == "schema":
        return _json_schema_check(actual, expected, base_dir)
    return False, f"unknown operator {op}"


def _describe_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def find(body: Any, path: str) -> list[Any]:
    from jsonpath_ng.ext import parse

    return [m.value for m in parse(path).find(body)]


def evaluate_one(spec: dict[str, Any], result: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    original = spec
    problems = validate_assertion(spec)
    if problems:
        return {"assertion": original, "passed": False, "message": "invalid assertion: " + "; ".join(problems)}
    spec = _canon(spec)
    custom = spec.get("message")
    out: dict[str, Any] = {"assertion": original}
    try:
        if "path" in spec:
            op = next(k for k in spec if k in VALUE_OPERATORS)
            matches = find(result.get("body"), str(spec["path"]))
            if spec.get("all"):
                if not matches:
                    ok, msg, actual = False, "path not found in response", None
                else:
                    fails = []
                    for i, value in enumerate(matches):
                        good, why = _apply(op, value, spec[op], base_dir, 1)
                        if not good:
                            fails.append({"index": i, "actual": value, "reason": why})
                    ok = not fails
                    msg = "ok" if ok else f"{len(fails)} of {len(matches)} matches failed"
                    actual = fails[:5] if fails else len(matches)
            else:
                actual = MISSING if not matches else (matches[0] if len(matches) == 1 else matches)
                ok, msg = _apply(op, actual, spec[op], base_dir, len(matches))
                actual = None if actual is MISSING else actual
            out.update(passed=ok, expected={op: spec[op]}, actual=actual, message=msg)
        elif "header" in spec:
            name = str(spec["header"]).lower()
            headers = {str(k).lower(): v for k, v in (result.get("headers") or {}).items()}
            value = headers.get(name, MISSING)
            op = next(k for k in spec if k != "header" and k not in ("message", "all"))
            ok, msg = _apply(op, value, spec[op], base_dir, 0 if value is MISSING else 1)
            out.update(passed=ok, expected={op: spec[op]}, actual=None if value is MISSING else value, message=msg)
        elif "fault" in spec:
            expected = spec["fault"]
            fault = result.get("fault")
            if expected in (None, False) or (isinstance(expected, str) and expected.lower() in ("none", "no", "yok")):
                ok = fault is None
                msg = "ok" if ok else f"unexpected fault: {(fault or {}).get('message')}"
            elif isinstance(expected, str) and expected.lower() in ("any", "var", "yes"):
                ok, msg = fault is not None, "ok" if fault is not None else "a fault was expected"
            else:
                exp = expected if isinstance(expected, dict) else {"code": expected}
                if fault is None:
                    ok, msg = False, "a fault was expected"
                else:
                    reasons = []
                    if exp.get("code") and str(exp["code"]).lower() not in str(fault.get("code") or "").lower():
                        reasons.append(f"code is {fault.get('code')}")
                    text = exp.get("message_contains") or exp.get("message")
                    if text and tr_fold(text) not in tr_fold(fault.get("message") or ""):
                        reasons.append("message does not contain expected text")
                    ok, msg = not reasons, "ok" if not reasons else "; ".join(reasons)
            out.update(passed=ok, expected={"fault": expected}, actual=fault, message=msg)
        elif "status" in spec:
            expected = spec["status"]
            status = result.get("status")
            if isinstance(expected, list):
                ok = status in [int(x) for x in expected]
                msg = "ok" if ok else f"status {status}"
            elif isinstance(expected, dict):
                ok, msg = _numeric(status, expected)
            else:
                ok = status == int(expected)
                msg = "ok" if ok else f"status {status}"
            out.update(passed=ok, expected={"status": expected}, actual=status, message=msg)
        elif "responseTimeMs" in spec:
            ok, msg = _numeric(result.get("elapsed_ms"), spec["responseTimeMs"])
            out.update(passed=ok, expected={"responseTimeMs": spec["responseTimeMs"]},
                       actual=result.get("elapsed_ms"), message=msg)
        elif "schema" in spec:
            ok, msg = _json_schema_check(result.get("body"), spec["schema"], base_dir)
            out.update(passed=ok, expected={"schema": spec["schema"]}, message=msg)
        elif "success" in spec:
            business = result.get("business")
            if business is None:
                ok, actual, msg = False, None, "'success' needs a profile service (no business success rule here)"
            else:
                actual = bool(business.get("success"))
                ok = actual is spec["success"]
                msg = "ok" if ok else f"business success is {str(actual).lower()}" + (
                    f": {'; '.join(map(str, business.get('errors') or []))}" if business.get("errors") else "")
            out.update(passed=ok, expected={"success": spec["success"]}, actual=actual, message=msg)
    except Exception as exc:  # noqa: BLE001 - an assertion must report, not crash the run
        out.update(passed=False, message=f"assertion error: {type(exc).__name__}: {exc}")
    if not out.get("passed") and custom:
        out["message"] = f"{custom} ({out.get('message')})"
    return out


def evaluate(specs: list[Any], result: dict[str, Any], base_dir: Path | None = None) -> list[dict[str, Any]]:
    return [evaluate_one(s if isinstance(s, dict) else {"invalid": s}, result, base_dir) for s in specs or []]
