"""CLI: serve the dashboard, run headless, purge, or start the stub LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

from .config import RunConfig, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loadlab")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="dashboard + control API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)

    run = sub.add_parser("run", help="headless run")
    run.add_argument("--config", required=True)
    run.add_argument("--json", action="store_true")

    purge = sub.add_parser("purge", help="manifest-scoped purge")
    purge.add_argument("--run", required=True)
    purge.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")

    runs = sub.add_parser("runs", help="list runs")

    ladder = sub.add_parser("ladder", help="run a capacity ladder (api or agent RAG)")
    ladder.add_argument("--config", required=True)
    ladder.add_argument("--levels", default="10,25,50")
    ladder.add_argument("--mode", choices=["api", "agent", "chat"], default="agent")
    ladder.add_argument("--duration", type=float, default=15)
    ladder.add_argument("--out", default="")

    stub = sub.add_parser("stub", help="deterministic OpenAI-compatible SSE stub")
    stub.add_argument("--host", default="0.0.0.0")
    stub.add_argument("--port", type=int, default=8099)

    env = sub.add_parser("env", help="apply/restore the test .env profile")
    env.add_argument("action", choices=["apply", "restore", "status"])
    env.add_argument("--env-file", default=str(__import__("pathlib").Path.home() / "dev/librechat/.env"))
    env.add_argument("--no-restart", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        from .api import app

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "stub":
        import uvicorn
        from .stubllm import app as stub_app

        uvicorn.run(stub_app, host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "run":
        from .engine import Engine
        from .ledger import Ledger
        from .store import Store
        import time

        cfg = load_config(args.config)
        store = Store(cfg.users, cfg.duration_s)
        ledger = Ledger(cfg)
        asyncio.run(Engine(cfg, store, ledger).run())
        if args.json:
            print(json.dumps(store.snapshot(), indent=2, default=str))
        else:
            print(f"run {ledger.run_id}: {len(store.samples)} requests, "
                  f"{sum(store.errors_by_class.values())} errors -> {ledger.dir}")
        return 0

    if args.cmd == "purge":
        from .purge import purge_run

        data = __import__("loadlab.ledger", fromlist=["Ledger"]).Ledger.load(args.run)
        cfg = RunConfig.model_validate(data["config"])
        from .config import Target

        cfg.target = Target()
        result = purge_run(cfg, args.run, dry_run=not args.apply)
        print(json.dumps({"steps": result.steps, "residue": result.residue, "ok": result.ok}, indent=2, default=str))
        return 0 if result.ok or not args.apply else 1

    if args.cmd == "ladder":
        from .engine import Engine
        from .ledger import Ledger
        from .store import Store
        from . import report as reportmod
        import time

        base = load_config(args.config)
        modes = {
            "api": {"rag": {"weight": 1, "think_ms": 150, "file_kb": 8}},
            "agent": {"agentrag": {"weight": 1, "think_ms": 150}},
            "chat": {"chatty": {"weight": 1, "think_ms": 150, "turns": 3}},
        }
        rows = []
        levels = [int(x) for x in args.levels.split(",") if x.strip()]
        for users in levels:
            cfg = base.model_copy(deep=True)
            cfg.users = users
            cfg.duration_s = args.duration
            cfg.name = f"ladder-{args.mode}-{users}"
            cfg.mix = {k: type(next(iter(base.mix.values())))(**v) for k, v in modes[args.mode].items()}
            store = Store(cfg.users, cfg.duration_s)
            ledger = Ledger(cfg)
            asyncio.run(Engine(cfg, store, ledger).run())
            path = reportmod.write(cfg, store, ledger.dir)
            snap = store.snapshot()
            cap = store.capacity(cfg.slo_p95_ms, cfg.max_error_rate)
            rows.append(
                {
                    "users": users,
                    "requests": snap["totals"]["requests"],
                    "errors": snap["totals"]["errors"],
                    "p95_ms": (snap["by_action"].get("rag") or snap["by_action"].get("agent_chat") or {}).get("p95_ms"),
                    "ttft_p95_ms": (snap["by_action"].get("agent_chat") or {}).get("ttft_p95_ms"),
                    "capacity_users": cap["capacity_users"],
                    "silent": sum(1 for p in store.probes if not p["ok"]),
                    "report": str(path),
                }
            )
            print(json.dumps(rows[-1]))
        out = args.out or f"runs/ladder-{args.mode}-summary.md"
        lines = [f"# RAG ladder — {args.mode} mode", "", "| users | requests | errors | p95 ms | ttft p95 | silent | capacity |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r['users']} | {r['requests']} | {r['errors']} | {r['p95_ms']} | {r['ttft_p95_ms']} | "
                f"{r['silent']} | {r['capacity_users']} |"
            )
        pathlib.Path(out).write_text("\n".join(lines) + "\n")
        print("summary:", out)
        return 0

    if args.cmd == "runs":
        from .ledger import Ledger

        for run_id in Ledger.runs():
            data = Ledger.load(run_id)
            print(f"{run_id:40s} {data.get('status'):10s} users={len(data.get('users', []))}")
        return 0

    if args.cmd == "env":
        from pathlib import Path
        from . import env as envmod

        if args.action == "apply":
            print(json.dumps(envmod.apply(Path(args.env_file), apply_restart=not args.no_restart), indent=2))
        elif args.action == "restore":
            print(json.dumps(envmod.restore(apply_restart=not args.no_restart), indent=2))
        else:
            print(json.dumps(envmod.status(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
