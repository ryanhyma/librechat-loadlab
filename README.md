# loadlab — LibreChat capacity & user-error harness

Runs **outside** LibreChat: a dashboard where you pick how many users and what each user does,
then measures throughput, latency and **every user-visible error** (including the ones LibreChat
reports as success).

> Production target: Windows Server, native services, **no containers**. Dev box: Linux + Docker.
> Same Python package, `--profile` is just configuration.

## What it captures

| Surface | Examples |
|---|---|
| Pre-stream HTTP | message/IP + message/user 429s, **concurrency 429** (`one message at a time`), upload 429s, 400/401/403/500 |
| In-stream SSE | `event: error`, `final{terminalStatus:"error"}`, `reconcile`, `abort`, stalls, disconnects |
| Client-synthesized | stream died / no content / conversation id never arrived (HTTP 200, broken for the user) |
| **Meili (silent)** | index lag: a just-created message is searched for and must appear; Meili fails without an error |
| **MCP (silent)** | tool output `Error: [tool] tool call failed: …` is detected instead of trusting HTTP 200 |
| **rag_api (silent)** | `/embed` returning 200 with `status:false`; `file_search`-style "no results or errors occurred" |

Every error carries a class + limiter label, is aggregated live, and is written to the run report.

## Quick start (dev box)

```bash
cd ~/dev/loadlab
./.venv/bin/python -m loadlab.cli serve --host 127.0.0.1 --port 8090
# stub LLM so app capacity is measurable without the GPU (port 8099)
./.venv/bin/python -m loadlab.cli stub --port 8099
# open http://127.0.0.1:8090
```

`scripts/stub.json` and `scripts/full.json` are ready-made scenarios. Headless:

```bash
./.venv/bin/python -m loadlab.cli run --config scripts/full.json --json
```

Dev wiring used here: LibreChat at `:3080`, Mongo `172.18.0.4:27017`, Meili `172.18.0.3:7700`,
rag_api `172.18.0.5:8000`, `librechat.yaml` gained a `loadlabstub` custom endpoint pointing at the
stub. Override with `LOADLAB_*` env vars (`LOADLAB_BASE_URL`, `LOADLAB_MONGO_URI`, …).

## Dashboard

- **Scenario**: users, ramp, duration, think time, endpoint/model, SLO p95, max error rate.
- **What each user does**: weights for `chatty` (multi-turn chat), `reader` (history + pagination),
  `search` (Meili), `rag` (upload + rag_api embed/query probe), `impatient` (abort), `auth` (churn).
- **Live**: rps, in-flight streams, error rate, users active, per-action p50/p95/tt99 + TTFT,
  errors by class and by limiter, probe results, recent errors, run history.
- **Report**: `runs/<id>/report.md` + `report.json` — capacity at SLO, first breach, bottleneck
  attribution inputs, escape hatch for paste into a ticket.

## Safety: run ledger + manifest purge

`runs/<id>/ledger.json` records every test user and every object a run owns. Purge is
manifest-scoped, idempotent, and snapshots documents to `deleted.jsonl` before deleting:

```bash
./.venv/bin/python -m loadlab.cli purge --run <run-id>          # dry run
./.venv/bin/python -m loadlab.cli purge --run <id> --apply      # delete
```

Order: Meili docs → rag_api `DELETE /documents` → uploads dir → Mongo (`files`, `messages`,
`conversations`, `transactions`, `balances`, `presets`, `sharedlinks`, `toolcalls`, `sessions`,
`users` last) → verify **zero residue** (Mongo counts, Meili hits, pgvector rows, files on disk).
LibreChat's own `deleteUserById` is **not** a cascade — never rely on it.

## Test `.env` profile (no containers = change management)

```bash
LOADLAB_RESTART_CMD="docker restart LibreChat" \
  ./.venv/bin/python -m loadlab.cli env apply --env-file ~/dev/librechat/.env
./.venv/bin/python -m loadlab.cli env restore   # restores exact bytes, proves SHA-256
```

On Windows use e.g. `LOADLAB_RESTART_CMD="powershell -c Restart-Service LibreChat"` or nssm.
If a restart window is unacceptable, run without it: expect 429s at `MESSAGE_IP_MAX`/min and read
`probe` mode results instead.

## Windows install (no containers)

```powershell
.\scripts\install-windows.ps1 -InstallDir C:\loadlab -PythonExe C:\Python312\python.exe
```

Creates a venv, installs requirements, writes `%ProgramData%\loadlab\loadlab.env`, and registers a
service with NSSM (or prints the `sc.exe` equivalent). Set there: `LOADLAB_BASE_URL`,
`LOADLAB_MONGO_URI`, `LOADLAB_MEILI_URL`, `LOADLAB_MEILI_KEY`, `LOADLAB_RAG_URL`,
`LOADLAB_METRICS_SECRET`, `LOADLAB_UPLOADS_DIR`, `LOADLAB_PG_DSN`.

## Layout

```
loadlab/
  config.py     pydantic config (run + target)
  engine.py     provisioning, VU pool, archetypes, silent probes
  client.py     auth, chat SSE, reads, search, upload, rag_api
  errs.py       error taxonomy
  store.py      samples, percentiles, windows, capacity knee
  collector.py  /metrics + Mongo + psutil
  purge.py      manifest purge + residue verification
  env.py        test profile apply/restore
  api.py        FastAPI control plane + dashboard
  ui/index.html single-file dashboard
  stubllm.py    deterministic OpenAI-compatible SSE stub
```

## Known limits

- Filters and rate-limit stores are in-process here (Redis off): multi-node behaviour is not exercised.
- RAG ingestion needs a configured embedding provider; on this dev box `/embed` answers 200 with
  `status:false` (missing OpenAI key) and loadlab reports it as `silent_rag` — which is the point.
- `rag` probe talks to rag_api directly; LibreChat's own `file_search` path requires an agent with
  the file-search tool (next step, see PLAN.md).
- Generator should run on a different host than the one under test.
