"""FastAPI control plane + dashboard."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from .collector import Collector
from .config import RunConfig, RUNS_DIR
from .engine import Engine
from .ledger import Ledger
from . import report
from .purge import purge_run
from .store import Store

UI = Path(__file__).parent / "ui" / "index.html"
app = FastAPI(title="loadlab")


class Runner:
    def __init__(self) -> None:
        self.cfg: RunConfig | None = None
        self.store: Store | None = None
        self.ledger: Ledger | None = None
        self.engine: Engine | None = None
        self.task: asyncio.Task | None = None
        self.stop_flag: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, cfg: RunConfig) -> dict[str, Any]:
        if self.running:
            raise HTTPException(409, "a run is already in progress")
        store = Store(cfg.users, cfg.duration_s)
        ledger = Ledger(cfg)
        engine = Engine(cfg, store, ledger)
        self.cfg, self.store, self.ledger, self.engine = cfg, store, ledger, engine
        self.stop_flag = asyncio.Event()
        self.task = asyncio.create_task(self._run())
        return {"run_id": ledger.run_id, "started_at": store.started_at}

    async def _run(self) -> None:
        assert self.cfg and self.store and self.engine and self.ledger and self.stop_flag
        collector = Collector(self.cfg, self.store)
        coll_task = asyncio.create_task(collector.run(self.stop_flag))
        try:
            await self.engine.run()
        finally:
            self.stop_flag.set()
            await asyncio.gather(coll_task, return_exceptions=True)
            path = report.write(self.cfg, self.store, self.ledger.dir)
            self.ledger.note(f"report: {path}")
            if self.ledger.data.get("status") == "running":
                self.ledger.finish("finished")

    async def stop(self) -> None:
        if not self.running or not self.engine:
            raise HTTPException(409, "no run in progress")
        await self.engine.shutdown()


runner = Runner()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(UI, headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
async def favicon():
    from fastapi.responses import Response

    return Response(status_code=204)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "running": runner.running, "run_id": runner.ledger.run_id if runner.ledger else None}


@app.get("/api/defaults")
async def defaults() -> dict[str, Any]:
    return RunConfig().model_dump()


@app.post("/api/runs")
async def start_run(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = RunConfig.model_validate(payload)
    return await runner.start(cfg)


@app.post("/api/runs/stop")
async def stop_run() -> dict[str, Any]:
    await runner.stop()
    return {"ok": True}


@app.get("/api/live")
async def live() -> StreamingResponse:
    async def gen():
        while True:
            if runner.store is None:
                yield "data: {\"idle\": true}\n\n"
                await asyncio.sleep(1)
                continue
            snap = runner.store.snapshot()
            snap["run_id"] = runner.ledger.run_id if runner.ledger else None
            yield f"data: {json.dumps(snap, default=str)}\n\n"
            if snap["run"]["finished"]:
                return
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/runs")
async def runs() -> list[dict[str, Any]]:
    out = []
    for run_id in Ledger.runs():
        try:
            data = Ledger.load(run_id)
        except Exception:
            continue
        out.append(
            {
                "run_id": run_id,
                "status": data.get("status"),
                "created_at": data.get("created_at"),
                "users": len(data.get("users", [])),
                "name": data.get("config", {}).get("name"),
                "has_report": (RUNS_DIR / run_id / "report.json").exists(),
            }
        )
    return sorted(out, key=lambda r: r["created_at"] or 0, reverse=True)


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    try:
        data = Ledger.load(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "unknown run")
    report_path = RUNS_DIR / run_id / "report.json"
    data["report"] = json.loads(report_path.read_text()) if report_path.exists() else None
    return data


@app.post("/api/purge")
async def purge(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = payload.get("run_id")
    dry = bool(payload.get("dry_run", True))
    if not run_id:
        raise HTTPException(400, "run_id required")
    try:
        data = Ledger.load(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "unknown run")
    cfg = RunConfig.model_validate(data["config"])
    from .config import Target

    cfg.target = Target()  # current environment wins over the stored one
    result = await asyncio.to_thread(purge_run, cfg, run_id, dry)
    return {
        "run_id": result.run_id,
        "dry_run": result.dry_run,
        "steps": result.steps,
        "residue": result.residue,
        "ok": result.ok,
    }


@app.get("/api/runs/{run_id}/report")
async def run_report(run_id: str) -> FileResponse:
    path = RUNS_DIR / run_id / "report.md"
    if not path.exists():
        raise HTTPException(404, "no report")
    return FileResponse(path, media_type="text/markdown")


@app.get("/api/runs/{run_id}/report.json")
async def run_report_json(run_id: str) -> FileResponse:
    path = RUNS_DIR / run_id / "report.json"
    if not path.exists():
        raise HTTPException(404, "no report")
    return FileResponse(path, media_type="application/json")
