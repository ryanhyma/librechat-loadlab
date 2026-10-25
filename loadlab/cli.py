"""CLI: serve the dashboard, run headless, purge, or start the stub LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
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
