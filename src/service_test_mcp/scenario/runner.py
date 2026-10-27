"""Runs scenarios step by step: render templates -> call service -> evaluate assertions -> extract variables."""

from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from ..config import ConfigError
from ..engine import Engine
from .asserts import evaluate, find
from .model import Scenario, ScenarioError, load_documents, load_file, parse_scenario, parse_text, scenario_files
from .template import Context, TemplateError

VERBOSITY = ("summary", "failures", "full")


class Runner:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- loading -------------------------------------------------------------------------------------------------
    def resolve_path(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute() and not p.exists() and self.engine.settings.scenario_root:
            p = Path(self.engine.settings.scenario_root) / p
        if not p.exists():
            raise FileNotFoundError(f"Scenario path not found: {p}")
        root = self.engine.settings.scenario_root
        if root and not str(p.resolve()).lower().startswith(str(Path(root).resolve()).lower()):
            raise PermissionError(f"Scenario path must be under scenario_root ({root})")
        return p

    def load(self, path: str | None = None, content: str | None = None) -> list[Scenario]:
        protocols = self.engine.protocols() if self.engine.settings.services else None
        if content is not None:
            docs = load_documents(parse_text(content))
            return [parse_scenario(d, protocols, source="(inline)", base_dir=Path.cwd()) for d in docs]
        if not path:
            raise ValueError("give 'path' (file or folder) or 'content' (YAML text)")
        p = self.resolve_path(path)
        out: list[Scenario] = []
        problems: list[str] = []
        for file in scenario_files(p):
            try:
                for doc in load_documents(load_file(file)):
                    out.append(parse_scenario(doc, protocols, source=str(file), base_dir=file.parent))
            except ScenarioError as exc:
                problems += [f"{file.name}: {x}" for x in exc.problems]
            except Exception as exc:  # noqa: BLE001 - YAML syntax errors etc.
                problems.append(f"{file.name}: {type(exc).__name__}: {exc}")
        if problems:
            raise ScenarioError(problems)
        return out

    def validate(self, path: str | None = None, content: str | None = None) -> dict[str, Any]:
        try:
            scenarios = self.load(path, content)
        except ScenarioError as exc:
            return {"valid": False, "problems": exc.problems}
        return {"valid": True, "scenarios": [{"id": s.id, "title": s.title, "steps": len(s.steps), "tags": s.tags,
                                              "source": s.source} for s in scenarios]}

    # -- running -------------------------------------------------------------------------------------------------
    def run_scenario(self, scenario: Scenario, environment: str | None = None,
                     variables: dict[str, Any] | None = None, verbosity: str = "failures") -> dict[str, Any]:
        engine = self.engine
        env = engine.settings.environment(environment or scenario.environment)
        ctx = Context(env_variables=engine.env_variables(env.name), on_secret=engine.masker.add_secret)
        started = time.perf_counter()
        report: dict[str, Any] = {"id": scenario.id, "title": scenario.title, "environment": env.name,
                                  "source": scenario.source, "passed": True, "steps": []}
        try:
            ctx.variables.update(ctx.render(scenario.variables))
            ctx.variables.update(variables or {})
        except TemplateError as exc:
            report.update(passed=False, error=str(exc), duration_ms=0)
            return report
        stop = False
        for step in scenario.steps:
            entry: dict[str, Any] = {"name": step.name, "service": step.service}
            if step.skip or stop:
                entry["status"] = "skipped"
                report["steps"].append(entry)
                continue
            outcome = self._run_step(step, ctx, env.name, scenario, entry)
            report["steps"].append(entry)
            if not outcome:
                report["passed"] = False
                if not step.continue_on_failure:
                    stop = True
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        report["summary"] = {k: sum(1 for s in report["steps"] if s.get("status") == k)
                             for k in ("passed", "failed", "error", "skipped")}
        return self._shape(report, verbosity)

    def _run_step(self, step: Any, ctx: Context, env: str, scenario: Scenario, entry: dict[str, Any]) -> bool:
        engine = self.engine
        raw = step.raw
        try:
            svc, _ = engine.resolve(step.service, env)
            if svc.name in engine.profiles:
                ctx.keep = ("persona", "session")
                try:
                    params = ctx.render(raw.get("params", raw.get("input")) or {})
                finally:
                    ctx.keep = ()
                persona = ctx.render(raw.get("persona") or scenario.persona)
                entry["operation"] = raw.get("operation")
                if persona:
                    entry["persona"] = persona
                entry["request"] = params
                result = engine.profile_call(svc.name, str(raw.get("operation")), params, persona, env,
                                             origin="scenario", fresh_session=bool(raw.get("fresh_session")))
                ctx.variables["persona"] = engine.profiles[svc.name].persona_values(env, persona)
            elif svc.protocol == "soap":
                params = ctx.render(raw.get("params", raw.get("input")) or {})
                entry["operation"] = raw.get("operation")
                entry["request"] = params
                result = engine.soap_call(svc.name, str(raw.get("operation")), params, env, origin="scenario")
            else:
                method = str(ctx.render(raw.get("method", "GET"))).upper()
                path = str(ctx.render(raw.get("path", "")))
                body = ctx.render(raw.get("body")) if "body" in raw else None
                body_text = ctx.render(raw.get("body_text")) if "body_text" in raw else None
                query = ctx.render(raw.get("query")) if raw.get("query") else None
                headers = ctx.render(raw.get("headers")) if raw.get("headers") else None
                form = ctx.render(raw.get("form")) if raw.get("form") else None
                entry["request"] = {"method": method, "path": path, "query": query,
                                    "body": body if body is not None else body_text or form}
                result = engine.rest_call(svc.name, method, path, query, headers, body, body_text, form, env,
                                          origin="scenario")
            assertions = ctx.render(step.assertions)
        except (TemplateError, ConfigError, ValueError, FileNotFoundError) as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
            return False
        entry["http_status"] = result.get("status")
        entry["elapsed_ms"] = result.get("elapsed_ms")
        if result.get("fault"):
            entry["fault"] = result["fault"]
        if result.get("error"):
            entry["transport_error"] = result["error"]
        if result.get("business") is not None:
            entry["business"] = result["business"]
        for flag in ("session_renewed", "session_expired"):
            if result.get(flag):
                entry[flag] = result[flag]
        checks = evaluate(assertions, result, scenario.base_dir)
        if not assertions:
            # no assertions: a fault / HTTP error / transport error fails the step
            ok = result.get("fault") is None and result.get("error") is None and \
                int(result.get("status") or 0) < 400 and (result.get("business") or {}).get("success", True)
            checks = [{"assertion": "(implicit) call succeeded", "passed": ok,
                       "message": "ok" if ok else "call failed and the step has no assertions"}]
        entry["assertions"] = checks
        passed = all(c.get("passed") for c in checks)
        extracted: dict[str, Any] = {}
        for var, path in step.extract.items():
            try:
                values = find(result.get("body"), path)
            except Exception as exc:  # noqa: BLE001
                entry.setdefault("extract_errors", []).append(f"{var}: {exc}")
                passed = False
                continue
            if not values:
                entry.setdefault("extract_errors", []).append(f"{var}: path {path} not found in response")
                passed = False
                continue
            extracted[var] = values[0] if len(values) == 1 else values
        ctx.variables.update(extracted)
        if extracted:
            entry["extracted"] = extracted
        entry["response"] = result.get("body")
        entry["status"] = "passed" if passed else "failed"
        return passed

    def _shape(self, report: dict[str, Any], verbosity: str) -> dict[str, Any]:
        if verbosity not in VERBOSITY:
            verbosity = "failures"
        for step in report["steps"]:
            failed = step.get("status") in ("failed", "error")
            if verbosity == "summary" or (verbosity == "failures" and not failed):
                step.pop("response", None)
                step.pop("request", None)
                if not failed:
                    checks = step.pop("assertions", [])
                    step["assertions_passed"] = len(checks)
            elif verbosity == "failures" and "assertions" in step:
                step["assertions"] = [c for c in step["assertions"] if not c.get("passed")]
        return report

    def run(self, path: str | None = None, content: str | None = None, environment: str | None = None,
            variables: dict[str, Any] | None = None, tags: list[str] | None = None, verbosity: str = "failures",
            junit_path: str | None = None, ids: list[str] | None = None) -> dict[str, Any]:
        scenarios = self.load(path, content)
        if tags:
            wanted = {t.lower() for t in tags}
            scenarios = [s for s in scenarios if wanted & {t.lower() for t in s.tags}]
        if ids:
            scenarios = [s for s in scenarios if s.id in ids]
        started = time.perf_counter()
        results = [self.run_scenario(s, environment, variables, verbosity) for s in scenarios]
        summary = {"scenarios": len(results), "passed": sum(1 for r in results if r["passed"]),
                   "failed": sum(1 for r in results if not r["passed"]),
                   "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                   "finished_at": _dt.datetime.now().replace(microsecond=0).isoformat()}
        out = {"summary": summary, "results": results}
        if junit_path:
            out["junit"] = write_junit(results, junit_path)
        return out


def write_junit(results: list[dict[str, Any]], path: str) -> str:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    failures = sum(1 for r in results if not r["passed"])
    total_s = sum(r.get("duration_ms", 0) for r in results) / 1000
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuite name="service-test-mcp" tests="{len(results)}" failures="{failures}" time="{total_s:.3f}">']
    for r in results:
        lines.append(f'  <testcase classname={quoteattr(r.get("source") or "inline")} name={quoteattr(r["id"])} '
                     f'time="{r.get("duration_ms", 0) / 1000:.3f}">')
        if not r["passed"]:
            reasons = []
            for s in r.get("steps", []):
                if s.get("status") in ("failed", "error"):
                    msgs = [c.get("message", "") for c in s.get("assertions", []) if not c.get("passed")]
                    reasons.append(f"{s['name']}: {s.get('error') or '; '.join(msgs) or s.get('status')}")
            if r.get("error"):
                reasons.append(r["error"])
            text = escape("\n".join(reasons))
            lines.append(f'    <failure message={quoteattr(reasons[0] if reasons else "failed")}>{text}</failure>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    target.write_text("\n".join(lines), encoding="utf-8")
    return str(target)
