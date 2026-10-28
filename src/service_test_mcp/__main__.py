"""Entry point.

    service-test-mcp                         MCP server over stdio (what VS Code starts)
    service-test-mcp --transport http        MCP server over streamable HTTP (http://127.0.0.1:8765/mcp)
    service-test-mcp --check [--service X]   configuration + connectivity report
    service-test-mcp --run scenarios/ [--env test] [--tags smoke] [--junit out.xml]
                                             run scenarios without any AI (CI / scheduled runs)
    service-test-mcp --import-log app.log [--service X] [--env prep] [--apply] [--scenario-out _taslak.yaml]
                                             traffic log -> catalog proposal (preview unless --apply), no AI needed
    service-test-mcp --demo-server           local demo SOAP + REST + mobile WCF services (port 8899)
    service-test-mcp --init                  copy the ready definitions (sistem, işlemler, yaklaşımlar, testler)
                                             into the definitions folder (existing files are kept)
    service-test-mcp --test "Kur bilgisi listeleme" --musteri 1000001 --env prep [--junit out.xml]
    service-test-mcp --test hepsi --tags duman --musteri 1000001
                                             run defined tests without any AI (CI / scheduled runs)
    service-test-mcp --schemas DIR           write the JSON Schemas of the definitions (for a UI)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__


def _print(obj: object) -> None:
    from .normalize import dumps

    text = json.dumps(json.loads(dumps(obj)), indent=2, ensure_ascii=False)
    try:
        print(text)
    except UnicodeEncodeError:  # old Windows consoles
        print(text.encode("ascii", "backslashreplace").decode("ascii"))


def _check(service: str | None, environment: str | None) -> int:
    from .engine import Engine

    report: dict = {"version": __version__, "python": sys.version.split()[0],
                    "bits": 64 if sys.maxsize > 2 ** 32 else 32}
    engine = Engine()
    report["config_file"] = engine.settings.path
    if engine.load_error:
        report["config_error"] = engine.load_error
        _print(report)
        return 2
    if not engine.settings.services and not engine.settings.broken:
        report["note"] = "No services configured (set STM_CONFIG or create %USERPROFILE%\\.service-test-mcp\\services.yaml)."
        _print(report)
        return 1
    report["environments"] = list(engine.settings.environments)
    report["default_environment"] = engine.settings.default_environment
    services: dict = {}
    names = [service] if service else list(engine.settings.services)
    for name in names:
        try:
            svc, env = engine.resolve(name, environment)
            if svc.protocol == "soap":
                info = engine.soap.describe_service(svc, env)
                services[svc.name] = {"ok": True, "protocol": "soap", "environment": env.name,
                                      "address": info["address"], "operations": len(info["operations"])}
            else:
                res = engine.rest.call(svc, env, "GET", "")
                services[svc.name] = {"ok": res.get("error") is None, "protocol": "rest", "environment": env.name,
                                      "http_status": res.get("status"), **({"error": res["error"]}
                                                                           if res.get("error") else {})}
        except Exception as exc:  # noqa: BLE001
            services[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    for name, why in engine.settings.broken.items():
        services[name] = {"ok": False, "error": why}
    report["services"] = engine.view(services)
    _print(report)
    return 0 if all(v.get("ok") for v in services.values()) else 1


def _run(path: str, environment: str | None, tags: str | None, junit: str | None, verbosity: str,
         report_path: str | None) -> int:
    from .engine import Engine
    from .scenario.model import ScenarioError
    from .scenario.runner import Runner

    engine = Engine()
    if engine.load_error:
        print(f"config error: {engine.load_error}", file=sys.stderr)
        return 2
    try:
        out = Runner(engine).run(path, None, environment, None,
                                 [t.strip() for t in tags.split(",")] if tags else None, verbosity, junit)
    except ScenarioError as exc:
        _print({"valid": False, "problems": exc.problems})
        return 2
    view = engine.view(out, 10_000_000)
    if report_path:
        from .normalize import dumps

        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(json.loads(dumps(view)), indent=2, ensure_ascii=False))
    for r in view["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {r['id']} ({r.get('duration_ms', 0):.0f} ms)")
        for s in r.get("steps", []):
            if s.get("status") in ("failed", "error"):
                print(f"       step '{s['name']}': {s.get('error') or s.get('status')}")
                for c in s.get("assertions", []):
                    if not c.get("passed"):
                        print(f"         - {json.dumps(c.get('assertion'), ensure_ascii=False, default=str)}: "
                              f"{c.get('message')} (actual: {json.dumps(c.get('actual'), ensure_ascii=False, default=str)[:200]})")
                for err in s.get("extract_errors", []):
                    print(f"         - extract {err}")
        if r.get("error"):
            print(f"       {r['error']}")
    s = view["summary"]
    print(f"\n{s['passed']}/{s['scenarios']} scenarios passed in {s['duration_ms'] / 1000:.1f}s"
          + (f" - JUnit: {out['junit']}" if out.get("junit") else ""))
    return 0 if s["failed"] == 0 and s["scenarios"] > 0 else 1


def _import_log(path: str, service: str | None, environment: str | None, apply: bool,
                scenario_out: str | None) -> int:
    from .engine import Engine
    from .importer.analyze import Importer

    engine = Engine()
    if engine.load_error:
        print(f"config error: {engine.load_error}", file=sys.stderr)
        return 2
    try:
        out = Importer(engine).run(path, None, service, environment, apply, scenario_out)
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    view = engine.view(out, 10_000_000)
    for rep in view.get("services", []):
        rep.pop("draft_scenario", None)
    _print(view)
    return 0 if out.get("matched") else 1


def _init(force: bool, kaynak: str | None = None) -> int:
    import shutil
    from importlib.resources import files
    from pathlib import Path

    from .tanim.depo import tanim_klasoru

    target = tanim_klasoru()
    source = Path(kaynak).expanduser() if kaynak else files("service_test_mcp").joinpath("ornek_tanimlar")
    if kaynak and not source.is_dir():
        print(f"kaynak klasör yok: {source}", file=sys.stderr)
        return 2
    copied, kept = [], []
    for folder in source.iterdir():
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            if not item.name.endswith(".yaml"):
                continue
            dest = target / folder.name / item.name
            if dest.exists() and not force:
                kept.append(str(dest))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with item.open("rb") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            copied.append(str(dest))
    _print({"tanim_klasoru": str(target), "kopyalanan": copied, "korunan (zaten vardi)": kept})
    return 0


def _schemas(directory: str) -> int:
    from pathlib import Path

    from .tanim.semalar import SEMALAR

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    for name, schema in SEMALAR.items():
        (out / f"{name}.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(SEMALAR)} schemas written to {out}")
    return 0


def _tests(name: str, customer: str | None, environment: str | None, tags: str | None, junit: str | None,
           report_path: str | None) -> int:
    from .engine import Engine
    from .tanim.depo import TanimHatasi
    from .tanim.kosucu import Kosucu

    engine = Engine()
    if engine.load_error:
        print(f"config error: {engine.load_error}", file=sys.stderr)
        return 2
    if engine.depo.yukleme_hatalari:
        for path, problems in engine.depo.yukleme_hatalari.items():
            print(f"tanım hatası {path}: {'; '.join(problems)}", file=sys.stderr)
    k = Kosucu(engine)
    if name.lower() in ("hepsi", "all", "*"):
        wanted = {t.strip().lower() for t in tags.split(",")} if tags else None
        names = [t.ad for t in engine.depo.hepsi("test") if t.veri.get("durum") == "onayli" and
                 (wanted is None or wanted & {x.lower() for x in t.veri.get("etiketler") or []})]
    else:
        names = [name]
    if not names:
        print("çalıştırılacak onaylı test yok", file=sys.stderr)
        return 2
    results = []
    for test in names:
        try:
            results.append(k.kos(test, None, customer, environment))
        except TanimHatasi as exc:
            results.append({"test": test, "sonuc": "hata", "mesaj": "; ".join(exc.problems)})
        except Exception as exc:  # noqa: BLE001
            results.append({"test": test, "sonuc": "hata", "mesaj": f"{type(exc).__name__}: {exc}"})
    view = engine.view(results, 10_000_000)
    for r in view:
        print(f"[{r['sonuc'].upper()}] {r['test']}" + (f" ({r.get('sure_ms', 0):.0f} ms)" if "sure_ms" in r else ""))
        if r.get("mesaj"):
            print(f"       {r['mesaj']}")
        for p in r.get("adimlar") or r.get("satirlar") or []:
            if p["sonuc"] != "geçti":
                label = p.get("adim") or p.get("durum")
                print(f"       {p['sonuc']}: {label} {p.get('mesaj') or ''}")
                for c in p.get("kalan_kontroller") or []:
                    print(f"         - {c['kontrol']}: {c.get('mesaj') or ''} (gerçek: "
                          f"{json.dumps(c.get('gercek'), ensure_ascii=False, default=str)[:200]})")
                if p["sonuc"] == "gözlem":
                    print(f"         cevap: {p.get('cevap')} {p.get('hatalar') or ''} -> onay bekliyor")
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(view, indent=2, ensure_ascii=False, default=str))
    if junit:
        from .scenario.runner import write_junit

        write_junit([{"id": r["test"], "source": "tanimlar", "passed": r["sonuc"] in ("geçti", "gözlem var"),
                      "duration_ms": r.get("sure_ms", 0),
                      "error": r.get("mesaj") or (None if r["sonuc"] in ("geçti", "gözlem var") else r["sonuc"]),
                      "steps": []} for r in view], junit)
    failed = sum(1 for r in view if r["sonuc"] in ("kaldı", "hata"))
    print(f"\n{len(view) - failed}/{len(view)} test geçti" + (f" - JUnit: {junit}" if junit else ""))
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service-test-mcp", description="MCP server for SOAP/WCF and REST testing")
    parser.add_argument("--version", action="version", version=f"service-test-mcp {__version__}")
    parser.add_argument("--check", action="store_true", help="check configuration and connectivity, then exit")
    parser.add_argument("--service", help="with --check: only this service; with --import-log: the service")
    parser.add_argument("--env", help="environment for --check / --run (default: default_environment)")
    parser.add_argument("--run", metavar="PATH", help="run scenario file/folder without an agent, then exit")
    parser.add_argument("--tags", help="with --run: comma separated tags")
    parser.add_argument("--junit", help="with --run: write a JUnit XML report")
    parser.add_argument("--report", help="with --run: write the full JSON report")
    parser.add_argument("--verbosity", default="failures", choices=("summary", "failures", "full"))
    parser.add_argument("--transport", default="stdio", choices=("stdio", "http"),
                        help="stdio (VS Code) or http (streamable HTTP for other agents)")
    parser.add_argument("--host", default=os.environ.get("STM_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STM_HTTP_PORT", "8765")))
    parser.add_argument("--init", action="store_true", help="copy the ready definitions into the definitions folder")
    parser.add_argument("--force", action="store_true", help="with --init: overwrite existing files")
    parser.add_argument("--kaynak", metavar="DIR", help="with --init: copy definitions from this folder (e.g. a "
                                                         "company package) instead of the built-in ones")
    parser.add_argument("--test", metavar="NAME", help="run a defined test ('hepsi' = every approved test)")
    parser.add_argument("--musteri", help="with --test: customer number")
    parser.add_argument("--schemas", metavar="DIR", help="write the JSON Schemas of the definitions")
    parser.add_argument("--import-log", metavar="FILE", help="read a traffic log and propose catalog changes")
    parser.add_argument("--apply", action="store_true", help="with --import-log: write the catalog")
    parser.add_argument("--scenario-out", help="with --import-log: write the draft scenario to this file")
    parser.add_argument("--demo-server", action="store_true", help="start the local demo services")
    parser.add_argument("--demo-port", type=int, default=8899)
    parser.add_argument("--log-level", default="WARNING", help="stderr log level (default WARNING)")
    args = parser.parse_args(argv)
    # stdout is the MCP channel - logs must go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.demo_server:
        from .demo import serve_forever

        serve_forever(args.demo_port)
        return 0
    if args.check:
        return _check(args.service, args.env)
    if args.run:
        return _run(args.run, args.env, args.tags, args.junit, args.verbosity, args.report)
    if args.init:
        return _init(args.force, args.kaynak)
    if args.schemas:
        return _schemas(args.schemas)
    if args.test:
        return _tests(args.test, args.musteri, args.env, args.tags, args.junit, args.report)
    if args.import_log:
        return _import_log(args.import_log, args.service, args.env, args.apply, args.scenario_out)
    if args.transport == "http":
        from .server import run_http

        run_http(args.host, args.port, os.environ.get("STM_HTTP_TOKEN") or None)
        return 0
    from .server import run_stdio

    run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
