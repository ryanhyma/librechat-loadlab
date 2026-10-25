# loadlab — status (2026-10-25, overnight build)

## TL;DR

Working end-to-end on the dev stack: dashboard, virtual users, archetype mix, live error taxonomy,
silent-failure probes (Meili/MCP/rag_api), run ledger, manifest purge with zero-residue proof,
deterministic stub LLM, test `.env` apply/restore with SHA proof, Windows install script.
29 test-data runs created and purged; Mongo has **0** leftover `@loadlab.invalid` users.

Run it:

```bash
cd ~/dev/loadlab
./.venv/bin/python -m loadlab.cli stub --port 8099            # terminal 1
./.venv/bin/python -m loadlab.cli serve --port 8090           # terminal 2
# http://127.0.0.1:8090  -> pick users + mix -> Start
```

## Verified (Playwright, browser console, real HTTP/SSE)

| Check | Result |
|---|---|
| Dashboard loads, no console errors (only the old favicon 404, now 204) | ok |
| Start run from the UI (8 users, mixed archetypes) | ok |
| Live tiles + per-action p50/p95 + TTFT + error tables + probes update via SSE | ok |
| Stop mid-run | ok — run marked `stopped`, report written |
| Run finishes on duration, start button re-enabled, runs list refreshes | ok |
| Report `report.md`/`report.json` written with capacity verdict | ok |
| Dry-run purge shows plan + residue; apply purge verifies zeros | ok (22 runs) |
| `.env` apply → restore → SHA-256 matches pre-run bytes | ok |
| Unit tests (taxonomy, percentiles, Prometheus parsing, store) | 9 passed |

Measured on dev (stub LLM = app-tier capacity, no GPU in the loop), 8 users / 15 s:

- 142 requests, 0 transport errors, ~9.6 rps
- chat p50 1373 ms / p95 1409 ms, TTFT p95 172 ms (stub: 120 ms TTFT + 48 tokens @ 60 tok/s)
- abort (impatient) p95 452 ms, TTFT 162 ms — real mid-stream abort, not a wrapper
- read p95 27 ms, search p95 85 ms, upload p95 45 ms
- Meili index lag 8–16 ms (probe passed)
- **rag_api embed now real**: local `all-MiniLM-L6-v2` via sentence-transformers → `status:true`,
  `/query` returns the fixture token, `rag_embed`/`rag_query` probes green (~20 ms embed, ~12 ms query)
- RAG vectors are purged per owner; a fresh run verified **all-zero residue** including pgvector
- real 27B: one turn ≈ 60 s (TTFT ≈ 54 s) → confirms why the stub tier is required for capacity work

## What's in the box

- `loadlab/engine.py` — provisioning, login pool, ramp, archetypes (`chatty`, `reader`, `search`,
  `rag`, `impatient`, `auth`), per-turn metrics, Meili + rag probes
- `loadlab/client.py` — bearer auth, chat POST + resumable SSE, abort, reads, search, upload, rag_api
- `loadlab/errs.py` — taxonomy: limiter classes, SSE terminal states, stream stall/disconnect,
  MCP tool-output errors, Meili/RAG silent failures
- `loadlab/collector.py` — LibreChat `/metrics` (bearer), Mongo counters, psutil host stats
- `loadlab/purge.py` — manifest purge, dry-run, snapshot-before-delete, residue verification
- `loadlab/env.py` — test profile apply/restore with backup + SHA-256 + restart command
- `loadlab/stubllm.py` — OpenAI-compatible SSE stub (TTFT/TPS/tokens, 500/429/disconnect modes)
- `loadlab/ui/index.html` — single-file dashboard (no build step)
- `scripts/install-windows.ps1` — venv + NSSM/sc.exe service
- `PLAN.md` — design + topology + error taxonomy + cleanup strategy

## Known gaps / next

1. **LibreChat `file_search` path** (agent with file-search tool) is not driven yet; the probe calls
   rag_api directly. Needs agent creation per run.
2. **redis off on dev** → limiters/pending requests are in-process; multi-node behaviour unverified.
3. Rate-limit profile is applied manually on dev (`.env` has raised limits + `METRICS_SECRET`);
   `env apply/restore` is implemented and tested, but the dev stack was not restarted by it.
4. Load ladder/soak presets exist as config only; no automatic "find the knee" sweep yet.
5. `docker restart LibreChat` is the dev restart path; production uses the `.env` `LOADLAB_RESTART_CMD`
   (nssm/sc.exe).
