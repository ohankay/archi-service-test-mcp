"""JSON Schema inference from samples, merging, and a readable diff (what changed in an operation's contract)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

MAX_DEPTH = 10
MAX_PROPERTIES = 300


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _types(schema: dict[str, Any]) -> list[str]:
    t = schema.get("type")
    return list(t) if isinstance(t, list) else ([t] if t else [])


def infer(value: Any, depth: int = 0) -> dict[str, Any]:
    t = _type(value)
    out: dict[str, Any] = {"type": t}
    if depth >= MAX_DEPTH:
        return out
    if t == "object":
        props = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_PROPERTIES:
                break
            props[str(k)] = infer(v, depth + 1)
        out["properties"] = props
        out["required"] = sorted(props)
    elif t == "array" and value:
        items: dict[str, Any] | None = None
        for v in value[:50]:
            items = infer(v, depth + 1) if items is None else merge(items, infer(v, depth + 1))
        out["items"] = items
    return out


def merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    if not a:
        return dict(b or {})
    if not b:
        return dict(a)
    types = sorted(set(_types(a)) | set(_types(b)))
    if "integer" in types and "number" in types:
        types.remove("integer")
    out: dict[str, Any] = {"type": types[0] if len(types) == 1 else types}
    if "properties" in a or "properties" in b:
        pa, pb = a.get("properties") or {}, b.get("properties") or {}
        out["properties"] = {k: merge(pa.get(k), pb.get(k)) for k in list(pa) + [k for k in pb if k not in pa]}
        ra = set(a.get("required") or []) if "properties" in a else set(pb)
        rb = set(b.get("required") or []) if "properties" in b else set(pa)
        out["required"] = sorted(ra & rb)
    if "items" in a or "items" in b:
        out["items"] = merge(a.get("items"), b.get("items"))
    return out


def diff(old: dict[str, Any] | None, new: dict[str, Any] | None, path: str = "$",
         out: list[str] | None = None) -> list[str]:
    """Changes from ``old`` (catalog) to ``new`` (this log's samples). Fields missing from a few samples are not
    reported as removed - absence in a log proves little - only additions and type changes are."""
    out = [] if out is None else out
    if not old or not new:
        return out
    to, tn = set(_types(old)), set(_types(new))
    added_types = tn - to - {"null"}
    if added_types and not (added_types == {"integer"} and "number" in to):
        out.append(f"{path}: type {'/'.join(sorted(to))} -> {'/'.join(sorted(tn))}")
    po, pn = old.get("properties") or {}, new.get("properties") or {}
    for key in pn:
        if key not in po and "properties" in old:
            out.append(f"{path}.{key}: new field ({'/'.join(_types(pn[key]))})")
        elif key in po:
            diff(po[key], pn[key], f"{path}.{key}", out)
    if old.get("items") and new.get("items"):
        diff(old["items"], new["items"], f"{path}[*]", out)
    return out


def fields(schema: dict[str, Any] | None) -> list[str]:
    """Top-level field names of an object schema (for short summaries)."""
    if not schema or "properties" not in schema:
        return []
    return sorted(schema["properties"])
