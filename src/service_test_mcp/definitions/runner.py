"""Runs operations and tests written as definitions.

* An operation is called by its friendly name with friendly inputs; defaults, date expressions (today-7), formats,
  fixed fields and inputs that come from another operation (``source``) are filled in here; login, session and the
  envelope are the profile's job.
* A test is a list of steps (``steps``) or a table (``operation`` + ``examples``). Checks are deterministic. The
  expected result of a step or example is ``success`` (default), ``error`` or ``?``: a ``?`` is not judged, it is
  reported as an observation for a person to confirm (then the test is updated and becomes deterministic).
* ``ai_review`` questions are returned with a summary of the response for the agent to answer; they never decide
  pass/fail.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..normalize import json_ready
from ..scenario.template import Context, TemplateError
from .checks import resolve, run_checks
from .compiler import template
from .schemas import EXAMPLE_KEYS
from .store import DefinitionError, Record
from .values import ValueProblem, convert, raw_value

if TYPE_CHECKING:
    from ..engine import Engine

SUMMARY_CHARS = 2500
RESULTS = ("passed", "failed", "observation", "error", "skipped")


class TestRunner:
    __test__ = False   # not a pytest class

    def __init__(self, engine: "Engine") -> None:
        self.e = engine
        self.store = engine.store
        self._sourced: dict[str, Any] = {}

    # -- helpers ------------------------------------------------------------------------------------------------
    def _environment(self, environment: str | None) -> str:
        return self.e.settings.environment(environment).name

    def _ctx(self, environment: str, variables: dict[str, Any]) -> Context:
        return Context(variables=variables, env_variables=self.e.env_variables(environment),
                       on_secret=self.e.masker.add_secret, keep=("test_customer", "session"),
                       secrets=self.e.secrets(environment))

    def _render(self, value: Any, environment: str, variables: dict[str, Any]) -> Any:
        return self._ctx(environment, variables).render(template(value))

    def summary(self, body: Any, limit: int = SUMMARY_CHARS) -> Any:
        view = self.e.view(body)
        text = json.dumps(json_ready(view), ensure_ascii=False, default=str)
        return view if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} characters)"

    # -- one operation ------------------------------------------------------------------------------------------
    def prepare_inputs(self, op: Record, given: dict[str, Any] | None, customer: str | None, environment: str,
                       variables: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """friendly inputs -> (service params, effective friendly values)."""
        defined = op.data.get("inputs") or {}
        values = self._render(dict(given or {}), environment, variables)
        unknown = [k for k in values if k not in defined]
        if unknown:
            raise DefinitionError([f"operation '{op.name}' has no input {', '.join(unknown)} "
                                   f"(inputs: {', '.join(defined) or '-'})"])
        params: dict[str, Any] = dict(self._render(op.data.get("fixed") or {}, environment, variables))
        effective: dict[str, Any] = {}
        for name, spec in defined.items():
            if name in values:
                value = values[name]
                if value is None:          # explicitly left out (negative tests: missing field)
                    effective[name] = None
                    continue
            elif "default" in spec:
                value = self._render(spec["default"], environment, variables)
            elif spec.get("source"):
                value = self._from_source(spec["source"], customer, environment, variables)
            elif spec.get("required"):
                raise DefinitionError([f"operation '{op.name}' needs input '{name}'"
                                       + (f" ({spec['description']})" if spec.get("description") else "")])
            else:
                continue
            raw, value = raw_value(value)
            if raw:
                sent = value                               # deliberately unconverted (negative tests)
            else:
                try:
                    sent = convert(value, str(spec.get("type") or "text"), spec.get("format"))
                except ValueProblem as exc:
                    raise DefinitionError([f"'{op.name}'.{name}: {exc}"]) from None
            params[str(spec["field"])] = sent
            effective[name] = sent
        return params, effective

    def _from_source(self, source: dict[str, Any], customer: str | None, environment: str,
                     variables: dict[str, Any]) -> Any:
        key = json.dumps([source, customer, environment], sort_keys=True, default=str)
        if key not in self._sourced:
            op = self.store.get("operation", source["operation"])
            result, _ = self.call(op, source.get("input"), customer, environment, variables)
            if not (result.get("business") or {}).get("success"):
                raise DefinitionError([f"'{source['operation']}' (input source) failed: "
                                       f"{(result.get('business') or {}).get('errors')}"])
            values = resolve(result.get("body"), source["extract"])
            if not values:
                raise DefinitionError([f"'{source['extract']}' is not in the response of '{source['operation']}'"])
            self._sourced[key] = values[0]
        return self._sourced[key]

    def call(self, op: Record, given: dict[str, Any] | None, customer: str | None, environment: str,
             variables: dict[str, Any] | None = None, origin: str = "test") -> tuple[dict[str, Any], dict]:
        system = self.store.system_of(op.data)
        params, effective = self.prepare_inputs(op, given, customer, environment, variables or {})
        result = self.e.profile_call(system.name, str(op.data["method"]), params,
                                     str(customer) if customer not in (None, "") else None, environment,
                                     origin=origin)
        return result, effective

    def error_paths(self, op: Record) -> list[str]:
        return self.e.profiles[self.store.system_of(op.data).name].profile.error_paths

    # -- tests --------------------------------------------------------------------------------------------------
    def test_data(self, test: str | None = None, content: Any = None) -> dict[str, Any]:
        if content is not None:
            import yaml

            data = yaml.safe_load(content) if isinstance(content, str) else content
            if not isinstance(data, dict):
                raise DefinitionError(["the test content must be a mapping"])
            problems = self.store.validate("test", data)
            if problems:
                raise DefinitionError(problems)
            return data
        if not test:
            raise DefinitionError(["give 'test' (a name) or 'content' (YAML)"])
        record = self.store.get("test", test)
        problems = self.store.validate("test", record.data)
        if problems:
            raise DefinitionError([f"'{record.name}' is invalid: " + "; ".join(problems)])
        return record.data

    def run(self, test: str | None = None, content: Any = None, customer: str | None = None,
            environment: str | None = None, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.test_data(test, content)
        environment = self._environment(environment or data.get("environment"))
        self._sourced = {}
        started = time.perf_counter()
        variables: dict[str, Any] = dict(self._render(data.get("variables") or {}, environment, {}))
        variables.update(inputs or {})
        report: dict[str, Any] = {"test": data["name"], "environment": environment, "customer": customer,
                                  "status": data.get("status")}
        if data.get("examples"):
            parts = self._table(data, customer, environment, variables)
            report["examples"] = parts
        else:
            parts = self._steps(data, customer, environment, variables)
            report["steps"] = parts
        counts = {s: sum(1 for p in parts if p["result"] == s) for s in RESULTS}
        report["summary"] = {k: v for k, v in counts.items() if v}
        report["result"] = "failed" if counts["failed"] or counts["error"] else (
            "observations" if counts["observation"] else "passed")
        if counts["observation"]:
            report["pending_review"] = ("Cases expected '?' are reported as observations. Ask the user whether the "
                                        "behaviour is right; then set 'expected' in the test to 'success' or 'error' "
                                        "(save_definition).")
        questions = [p.pop("_ai") for p in parts if p.get("_ai")]
        if questions:
            report["ai_review"] = questions
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return report

    def _judge(self, op: Record, expected: str, extra_checks: list[str], result: dict[str, Any],
               environment: str, variables: dict[str, Any]) -> dict[str, Any]:
        if result.get("error"):
            return {"result": "error", "message": f"service not reached: {result['error'].get('message')}"}
        succeeded = bool((result.get("business") or {}).get("success"))
        out: dict[str, Any] = {"response": "success" if succeeded else "failure"}
        if not succeeded:
            out["errors"] = (result.get("business") or {}).get("errors")
        out["duration_ms"] = result.get("elapsed_ms")
        if expected == "?":
            out["result"] = "observation"
            return out
        if expected == "error":
            lines = ["failure"] + list(extra_checks)
        else:
            lines = ["success"] + list(op.data.get("checks") or []) + list(extra_checks)
        try:
            lines = [str(x) for x in self._render(lines, environment, variables)]
        except TemplateError as exc:
            return {**out, "result": "error", "message": str(exc)}
        checks = run_checks(lines, result, self.error_paths(op))
        failed = [c for c in checks if not c["passed"]]
        out["result"] = "passed" if not failed else "failed"
        if failed:
            out["failed_checks"] = self.e.view(failed)
            out["response_summary"] = self.summary(result.get("body"))
        else:
            out["checks"] = len(checks)
        return out

    def _steps(self, data: dict[str, Any], customer: str | None, environment: str,
               variables: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        stop = False
        for step in data["steps"]:
            entry: dict[str, Any] = {"step": step.get("name") or step["operation"], "operation": step["operation"]}
            if stop:
                entry["result"] = "skipped"
                out.append(entry)
                continue
            try:
                op = self.store.get("operation", step["operation"])
                result, effective = self.call(op, step.get("input"), customer, environment, variables)
            except (DefinitionError, TemplateError, ValueError) as exc:
                entry.update(result="error", message=str(exc))
                out.append(entry)
                stop = True
                continue
            entry["input"] = self.e.view(effective)
            local = {**variables, **{k: v for k, v in effective.items() if v is not None}}
            entry.update(self._judge(op, step.get("expected", "success"), step.get("checks") or [], result,
                                     environment, local))
            for name, path in (step.get("extract") or {}).items():
                values = resolve(result.get("body"), path)
                if values:
                    variables[name] = values[0]
                elif entry["result"] == "passed":
                    entry.update(result="failed", message=f"extract: '{path}' is not in the response")
            if step.get("ai_review"):
                entry["_ai"] = {"step": entry["step"], "question": step["ai_review"],
                                "data": self.summary(result.get("body"))}
            out.append(entry)
            if entry["result"] in ("failed", "error"):
                stop = True
        return out

    def _table(self, data: dict[str, Any], customer: str | None, environment: str,
               variables: dict[str, Any]) -> list[dict[str, Any]]:
        op = self.store.get("operation", data["operation"])
        out: list[dict[str, Any]] = []
        for row in data["examples"]:
            inputs = {**(data.get("common_input") or {}),
                      **{k: v for k, v in row.items() if k not in EXAMPLE_KEYS}}
            entry: dict[str, Any] = {"case": row["case"], "expected": row["expected"]}
            try:
                result, effective = self.call(op, inputs, customer, environment, variables)
            except (DefinitionError, TemplateError, ValueError) as exc:
                entry.update(result="error", message=str(exc))
                out.append(entry)
                continue
            entry["input"] = self.e.view(effective)
            local = {**variables, **{k: v for k, v in effective.items() if v is not None}}
            extra = list(data.get("checks") or []) + list(row.get("checks") or []) if row["expected"] == "success" \
                else list(row.get("checks") or [])
            entry.update(self._judge(op, row["expected"], extra, result, environment, local))
            if data.get("ai_review"):
                entry["_ai"] = {"case": row["case"], "question": data["ai_review"],
                                "data": self.summary(result.get("body"), 800)}
            out.append(entry)
        return out
