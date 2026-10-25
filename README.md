# librechat-loadlab — capacity & user-error harness for LibreChat

Runs **outside** LibreChat: a dashboard where you pick how many users and what each user does,
then measures throughput, latency and **every user-visible error** (including the ones LibreChat
reports as success).

> Production target: Windows Server, native services, **no containers**. Dev box: Linux + Docker.
> Same Python package, `--profile` is just configuration.

Not affiliated with the LibreChat project. MIT licensed. Package/import name stays `loadlab`.

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

## Setup

Full instructions for both runtimes: **[docs/setup.md](docs/setup.md)** — container (dev) and native
Windows venv (production). Short version below.

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
agents) → **users last**, and only after every other surface verifies zero (fail-closed). A failed
dependency leaves the users in place and returns `retryable`; re-running continues. Abandoned runs are
swept with `purge --stale` (also on `serve` startup) and a test `.env` left applied is restored by
`env guard` — schedule both (§5 of docs/setup.md).
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

## Container

```bash
cp .env.example .env         # set LibreChat/Mongo/Meili/pg URLs + keys
docker compose up -d --build # dashboard :8090, stub LLM :8099
```

## Windows (native, no containers)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example "$env:ProgramData\loadlab\loadlab.env"
$env:LOADLAB_ENV_FILE="$env:ProgramData\loadlab\loadlab.env"
.\.venv\Scripts\python.exe -m loadlab.cli serve --host 0.0.0.0 --port 8090
```

Or `scripts\install-windows.ps1` to create the venv, config and an NSSM service. See
[docs/setup.md](docs/setup.md) for the service, firewall, `env apply/restore` and troubleshooting.

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
- RAG needs an embedding provider. Dev now uses local `all-MiniLM-L6-v2`; when embeddings are
  unavailable `/embed` still answers 200 with `status:false` and loadlab reports `silent_rag`.
- Two RAG paths are implemented and laddered: direct rag_api (`rag` archetype) and LibreChat agent
  `file_search` (`agentrag`, per-user agent + fixture + answer must contain the codename).
- Upstream 429s are masked by LibreChat (retry/stall). Count them at a gateway
  (`litellm_proxy_failed_requests_metric_total{exception_status="429"}`) or with the provider probe;
  LiteLLM is optional and only needed for quota/retry rehearsal.
- Generator should run on a different host than the one under test (dev shares the box, so p95 is a floor).

## Local HuggingFace embeddings for rag_api (no OpenAI key)

Dev (containers): `~/dev/librechat` now has `Dockerfile.rag-embeddings` + `docker-compose.embeddings.yml`,
which install CPU torch + sentence-transformers into the lite RAG image. Run the stack with:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.embeddings.yml up -d
```

`.env`: `EMBEDDINGS_PROVIDER=huggingface`, `EMBEDDINGS_MODEL=sentence-transformers/all-MiniLM-L6-v2`.
Model is 384-dim; pgvector's `embedding` column is unconstrained (`vector`), so no schema change.
First embed downloads ~90 MB into `HF_HOME` (`/app/data/hf`).

Windows prod (native rag_api):
```powershell
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install langchain-huggingface sentence-transformers "pandas>=2.2.2"
$env:EMBEDDINGS_PROVIDER="huggingface"
$env:EMBEDDINGS_MODEL="sentence-transformers/all-MiniLM-L6-v2"
$env:HF_HOME="C:\rag-api\data\hf"
```
Pre-download the model once (`python -c "from sentence_transformers import SentenceTransformer as S; S('sentence-transformers/all-MiniLM-L6-v2')"`)
so the service start is offline-safe. Verified on dev: `/embed` → `status:true`, `/query` returns the
fixture token, and `loadlab` reports `rag_embed ok` (no more `silent_rag`).
