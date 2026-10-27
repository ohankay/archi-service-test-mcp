"""Normalization rules - SOAP and REST responses end up in the same shape so assertions use one path syntax.

* SOAP (zeep) objects become dicts keyed by the element's local name (namespaces dropped).
* Elements that may repeat (maxOccurs > 1) are always lists, even with one item (zeep behaviour).
* xsd:decimal / JSON numbers with a fraction are ``Decimal`` internally (no float rounding in assertions).
* Dates and times become ISO-8601 strings; ``base64Binary`` becomes a base64 string; ``xsd:any`` content an XML string.
* Missing optional elements are ``null``.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


def normalize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, str, Decimal)):
        return obj
    if isinstance(obj, float):
        return Decimal(repr(obj))
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, Mapping):
        return {str(k): normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [normalize(v) for v in obj]
    tag = getattr(obj, "tag", None)
    if tag is not None and hasattr(obj, "iterchildren"):  # lxml element (xsd:any)
        from lxml import etree

        return etree.tostring(obj, encoding="unicode")
    if hasattr(obj, "__values__"):  # zeep CompoundValue that escaped serialize_object
        return {k: normalize(v) for k, v in obj.__values__.items()}
    return str(obj)


def _decimal_out(d: Decimal) -> Any:
    if d == d.to_integral_value() and abs(d) < Decimal(10) ** 15 and "E" not in str(d):
        return int(d)
    f = float(d)
    return f if Decimal(repr(f)) == d else str(d)


def json_ready(obj: Any) -> Any:
    """Copy safe for json.dumps: Decimals become numbers when exact, strings otherwise."""
    if isinstance(obj, Decimal):
        return _decimal_out(obj)
    if isinstance(obj, dict):
        return {k: json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_ready(v) for v in obj]
    return obj


def parse_json_text(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def dumps(obj: Any) -> str:
    return json.dumps(json_ready(obj), ensure_ascii=False, default=str, separators=(",", ":"))
