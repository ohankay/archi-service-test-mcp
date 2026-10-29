"""JSON Schemas of the four definition kinds: system, operation, approach, test.

The same schemas validate what the agent saves (save_definition) and will drive the forms of the UI.
``service-test-mcp --schemas DIR`` writes them as JSON files.
"""

from __future__ import annotations

from typing import Any

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_MAP = {"type": "object"}
_STATUS = {"type": "string", "enum": ["draft", "approved"], "description": "draft: made by an agent or a person, not "
           "reviewed yet. approved: runs in CI and in tag runs."}
_CHECKS = {"type": "array", "items": {"type": "string"},
           "description": "Check lines (e.g. 'success', 'count X >= 1', 'for each L: A <= B')"}
_EXPECTED = {"type": "string", "enum": ["success", "error", "?"]}

STEP = {
    "type": "object", "additionalProperties": False, "required": ["method"],
    "properties": {"method": _STR, "path": _STR, "input": _MAP, "description": _STR,
                   "extract": {"type": "object", "additionalProperties": _STR,
                               "description": "session variable name -> field path in the response"}},
}

SYSTEM: dict[str, Any] = {
    "title": "System", "type": "object", "additionalProperties": False, "required": ["name", "environments"],
    "properties": {
        "name": _STR, "description": _STR, "status": _STATUS,
        "environments": {"type": "object", "minProperties": 1, "additionalProperties": {
            "type": "object", "additionalProperties": False, "required": ["address"],
            "properties": {"address": _STR, "description": _STR, "variables": _MAP,
                           "allow_mutating": {"type": "boolean", "description": "operations such as money transfers"},
                           "default_customer": {"type": ["string", "integer"]},
                           "timeout_s": {"type": "number", "minimum": 1,
                                         "description": "longest wait for a response (default 30)"},
                           "proxy": {**_STR, "description": "e.g. http://proxy.company.local:8080; 'none' = connect "
                                     "directly; 'auto' = like the browser, from the Windows PAC setting"},
                           "verify_tls": {"type": ["boolean", "string"],
                                          "description": "true / false / path of the company CA file (.pem)"}}}},
        "variables": {**_MAP, "description": "Variables shared by all environments ({{env.x}})"},
        "request": {"type": "object", "additionalProperties": False, "properties": {
            "method": _STR, "url_pattern": _STR, "envelope": _MAP, "session_envelope": _MAP, "headers": _MAP}},
        "success": {**_CHECKS, "description": "Business-level success rule (e.g. 'Success = true')"},
        "error_fields": _STRS,
        "session": {"type": "object", "additionalProperties": False, "properties": {
            "ttl_s": {"type": "number", "minimum": 1}, "refresh_margin_s": {"type": "number", "minimum": 0},
            "ttl_mode": {"type": "string", "enum": ["absolute", "idle"]},
            "defaults": _MAP, "login": {"type": "array", "items": STEP}, "logout": STEP,
            "expired_when": _CHECKS, "retry_mutating": {"type": "boolean"}}},
        "customer": {"type": "object", "additionalProperties": False, "properties": {
            "field": {**_STR, "description": "{{customer.<field>}} - default: no"}}},
        "noise": _STRS,
        "secret_values": {"type": "object", "additionalProperties": {
            "type": "object", "required": ["method", "field"], "additionalProperties": False,
            "properties": {"method": _STR, "field": _STR}},
            "description": "Values copied from a log into the local secrets.yaml by import_log"},
        "log_fields": {"type": "object", "additionalProperties": False,
                       "properties": {"customer": _STRS, "secret": _STRS}},
        "hook": {**_STR, "description": "Python class for logic YAML cannot express: file.py:Class "
                 "(prepare(request, ctx) / inspect(result, ctx))"},
    },
}

INPUT = {
    "type": "object", "additionalProperties": False, "required": ["field"],
    "properties": {
        "field": {**_STR, "description": "Field name sent to the service"},
        "type": {"type": "string", "enum": ["text", "integer", "decimal", "date", "boolean", "list"]},
        "required": {"type": "boolean"}, "default": {}, "format": _STR, "description": _STR,
        "min": {}, "max": {}, "options": {"type": "array"},
        "source": {"type": "object", "additionalProperties": False, "required": ["operation", "extract"],
                   "properties": {"operation": _STR, "extract": _STR, "input": _MAP},
                   "description": "When not given, call this operation and take the value from its response"},
    },
}

OPERATION: dict[str, Any] = {
    "title": "Operation", "type": "object", "additionalProperties": False, "required": ["name", "method"],
    "properties": {
        "name": _STR, "description": _STR, "system": _STR, "method": _STR, "path": _STR, "http_method": _STR,
        "kind": {"type": "string", "enum": ["read", "mutating", "auth", "unknown"]},
        "session": {"type": "boolean"}, "status": _STATUS, "tags": _STRS,
        "inputs": {"type": "object", "additionalProperties": INPUT},
        "fixed": {**_MAP, "description": "Fields sent unchanged with every call"},
        "date_range": {"type": "object", "additionalProperties": False, "required": ["start", "end"],
                       "properties": {"start": _STR, "end": _STR, "max_days": {"type": "integer", "minimum": 0}}},
        "checks": {**_CHECKS, "description": "Checks that hold for every call of this operation"},
        "source_log": _STR,
    },
}

EXAMPLE = {"type": "object", "required": ["case", "expected"], "properties": {
    "case": _STR, "expected": _EXPECTED, "checks": _CHECKS, "note": _STR}}
EXAMPLE_KEYS = ("case", "expected", "checks", "note")   # every other key of a row is an input

TEST_STEP = {
    "type": "object", "additionalProperties": False, "required": ["operation"],
    "properties": {"name": _STR, "operation": _STR, "input": _MAP, "checks": _CHECKS, "ai_review": _STR,
                   "extract": {"type": "object", "additionalProperties": _STR}, "expected": _EXPECTED},
}

TEST: dict[str, Any] = {
    "title": "Test", "type": "object", "additionalProperties": False, "required": ["name"],
    "properties": {
        "name": _STR, "description": _STR, "tags": _STRS, "status": _STATUS, "environment": _STR,
        "variables": _MAP,
        "steps": {"type": "array", "items": TEST_STEP, "minItems": 1},
        "operation": {**_STR, "description": "Table test: one operation, every example is a case"},
        "examples": {"type": "array", "items": EXAMPLE, "minItems": 1},
        "common_input": _MAP, "checks": _CHECKS, "ai_review": _STR,
        "approach": _STR, "generated_by": {"type": "string", "enum": ["manual", "code", "agent"]},
    },
    "oneOf": [{"required": ["steps"], "not": {"required": ["examples"]}},
              {"required": ["operation", "examples"], "not": {"required": ["steps"]}}],
}

APPROACH: dict[str, Any] = {
    "title": "Test approach", "type": "object", "additionalProperties": False, "required": ["name", "generator"],
    "properties": {
        "name": _STR, "aliases": _STRS, "description": _STR, "when_to_use": _STR, "status": _STATUS,
        "generator": {"type": "string", "enum": ["boundary_value", "equivalence", "pairwise", "negative", "agent"],
                      "description": "all but 'agent' are generated by code, deterministically"},
        "rules": {**_STRS, "description": "for the agent generator: the rules to write the tests by"},
        "limit": {"type": "integer", "minimum": 1, "description": "most cases to generate"},
        "expected_outside": {"type": "string", "enum": ["error", "?"],
                             "description": "expected result for values outside a limit"},
    },
}

SCHEMAS = {"system": SYSTEM, "operation": OPERATION, "test": TEST, "approach": APPROACH}
FOLDERS = {"system": "systems", "operation": "operations", "test": "tests", "approach": "approaches"}
KINDS = tuple(FOLDERS)
