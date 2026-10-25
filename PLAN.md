# loadlab — LibreChat load & user-error harness

Status: **BUILT and validated** (2026-10-25) — see `STATUS.md`; setup in `docs/setup.md`.
This file remains the design rationale (error surfaces, topology, cleanup strategy).
Target: Windows Server (Python 3.12 + Node installed, containers allowed). Library first, container optional.

## 0. Two runtimes — same code, different hosts

| | **dev (this box)** | **prod (target)** |
|---|---|---|
| Host | Linux + Docker | Windows Server, **native services, no containers** |
| LibreChat | Docker Compose stack | native Node service |
| Mongo / Meili / Postgres | containers | native services, one role per server |
| loadlab | container or venv | Python venv + Windows service (NSSM or Task Scheduler) |
| destructive tests | throwaway second compose stack | not run in prod — see 5.3 |

Rule: **loadlab core must not assume Docker.** Container stats are an optional plugin (`docker`
extra), host stats come from `psutil`. Every path via `pathlib.Path`, every service action behind a
`ServiceControl` interface (`systemd` | `nssm` | `sc.exe`), every probe plain TCP/HTTP so nothing
depends on `mongosh`, `psql` or shell tooling on Windows.

Windows-specific implementation notes:
- `asyncio` proactor loop by default; no `fork` — one process, thread pool only for DNS/fs.
- Graceful stop via `CTRL_BREAK_EVENT` / service stop, not `SIGTERM`; ledger flushed on stop.
- Long-lived SSE + Windows Defender/firewall: document the outbound rule for the generator box.
- Meili/Mongo/Postgres are Windows services — probe with `pymongo`, `httpx`, `psycopg`; never shell out.
- Timezone: Meili/Windows event logs in local time, reports normalised to UTC with the offset kept.

## 1. Goal

A dashboard, **outside LibreChat**, where you choose:

- **how many concurrent users** (plus ramp shape and duration),
- **what each user does** (traffic mix of archetypes: chat, follow-ups, RAG upload, reads, search, abort, auth churn),
- **which model path** (deterministic stub LLM or the real provider),

and it reports **every user-visible error**, not just HTTP 5xx: which limiter fired, which step failed,
whether the error reached the transcript or was swallowed, plus latency/capacity numbers and the
saturation knee.

Non-goal: replacing LibreChat's admin panel or the `virtUOS/librechat_exporter` (we scrape both).

## 2. What "user error" means in LibreChat (from source)

Three independent error surfaces. The harness must capture all three and reconcile them — a mismatch
is itself a finding.

### 2.1 Pre-stream HTTP errors (POST /api/agents/chat/:conversationId)

| Source | Status | Body shape |
|---|---|---|
| message IP limiter (`MESSAGE_IP_MAX`, default 40/min) | 429 | `{type, max, limiter:"ip", windowInMinutes, resetAt}` via `denyRequest` |
| message user limiter (`MESSAGE_USER_MAX`) | 429 | same, `limiter:"user"` |
| **concurrency limiter** (`LIMIT_CONCURRENT_MESSAGES`, 1 in-flight/user) | **429** | `getViolationInfo()` → `{type:"concurrent", limit, pendingRequests, score}` (`api/server/controllers/agents/request.js:1528`) |
| file upload limiters (`FILE_UPLOAD_IP_MAX` 100/15min, `_USER_MAX` 50/15min) | 429 | `{message:"Too many file upload requests…"}` |
| login/register limiters | 429 | limiter-specific message |
| ban / ACL / validation / auth | 403 / 400 / 401 | `{error|message}` |

The POST is normally **answered with generation JSON (incl. `streamId`)**, and the token stream is a
separate `GET /api/agents/chat/stream/:streamId` SSE subscription — so 429s can arrive at *either*
step and must be attributed to the right one.

### 2.2 In-stream SSE errors (during generation)

- `event: error` + `data: <string|object>` — `handleError()` in `packages/api/src/utils/events.ts`
  writes it and closes the stream.
- `event: message` rows carrying `error: true` / `error: "<text>"` (message-level error, persisted).
- `event: final` with `terminalStatus: "complete"|"error"|"aborted"`, optional top-level
  `error: {message}`, and `reconcile` + `reconcileReason`
  (`terminal_payload_missing`, `generation_replaced`, `abort_persistence_failed`).
- `event: abort` (user or server abort), `aborted` / `earlyAbort` flags.
- Stream-level Redis job events: `created`, `sync`, `title`, `attachment`, `tool`, `usage`, `done`.

### 2.3 Client-synthesized errors (no server error exists)

The UI invents error rows when the stream dies (`CONNECTION_ERROR_TEXT` in
`client/src/hooks/SSE/turns.ts`), when the conversation id never arrives, and on reconnect failures.
**These are real user-facing failures with a 200 on the wire.** Only the client path can see them, so
the harness must model "stream died mid-response" as its own class.

### 2.4 Persisted evidence (verification layer)

- `messages` rows with `error: true` — written by `sendError`/`denyRequest`.
- `transactions` — rejected requests by reason (`token_balance`, `illegal_model_request`, …).
- `files` — `embedded` false / missing RAG vector copy after a 429 during embedding.
- API `/metrics`: `http_requests_total{status}`, `sse_streams_total{status}`, `upload_*`,
  `generation_stream_recoveries_total`, `generation_stream_early_buffer_overflows_total`.
- `virtUOS/librechat_exporter`: `librechat_error_messages_total`, `_rejected_requests{reason}`,
  `_window_*`.

### 2.5 Silent-failure surfaces (Meili, MCP, rag_api) — status codes are not enough

These three fail **without an error status reaching the user**, so the harness needs ground-truth
probes in addition to transport capture.

**Meilisearch (conversation/message search)**
- Indexing is a *detached background operation* (`runDetachedMeiliOperation` in
  `packages/data-schemas/src/models/plugins/mongoMeili.ts`) — a failed index write is logged, the API
  still returns 200, and the user simply gets stale/empty search results.
- `api/db/indexSync.js` catches and only `logger.debug`s index-check failures.
- `/api/search/enable` is the only search-status route (health only). Search itself is
  `GET /api/convos?search=` and `GET /api/messages?search=`.
- Probe: create a marker conversation/message, then poll both search endpoints for the marker and
  measure **index lag**; fail the run if not findable within N seconds. Optionally poll Meili
  `/health` + `/tasks?statuses=failed` directly (port 7700) and count failed tasks per run.

**MCP**
- Tool failures are not HTTP errors: the tool output becomes a string
  `Error: [<tool>] tool call failed: <msg>` (`packages/api/src/agents/handlers.ts`) which the model may
  then summarise away, and `execute_code`-style blocks can be filtered as model-bound output.
- Connection/auth failures surface as typed classes (`MCPTransportAuthenticationError`,
  `MCPDomainNotAllowedError`, `MCPOAuthSecretReentryRequiredError`, `MCPTokenRefreshUnavailableError`, …
  in `packages/api/src/mcp/errors.ts`) and via `/api/mcp/servers`, `/api/mcp/tools`,
  `/api/mcp/connection/status[/:server]`.
- Separate 429 bucket: `mcpAppAdmissionLimiter`, `mcpAppResourceLimiter`, `mcpAppToolCallLimiter`,
  `toolCallLimiter` (1s window).
- Probe: call a known-good MCP tool (`list_templates` on the `ppt` server) and assert the output is not
  an error string; snapshot `/api/mcp/connection/status` before/after each run; count tool-call 429s.

**rag_api**
- `uploadVectors` throws with `File embedding failed.` / `File embedding failed. The filetype X is not
  supported` / `RAG_API_URL not defined` (`api/server/services/Files/VectorDB/crud.js`).
- `file_search` **swallows** HTTP errors: a failed `/query` becomes the tool result
  "No results found or errors occurred while searching the files." (`packages/api/src/files/rag/search.ts`)
  — a normal, successful tool call from the client's point of view.
- Probe: upload a fixture containing a unique random token, require `embedded: true`, then run a chat
  whose correct answer is that token. Missing token + "No results found…" ⇒ classify
  `rag_query_swallowed`. Also poll rag_api `/health` and pgvector chunk counts per file directly.

**Consequence for the dashboard:** the error report has two axes — **transport errors** (HTTP/SSE)
and **functional failures** (silent). A run can look 100% green on status codes and still have broken
search, broken RAG and broken MCP; the report shows both, side by side.

## 3. Architecture

```
┌──────────────────────────── loadlab (Windows host or container) ───────────────────────────┐
│  dashboard (static HTML/JS, no build step)  ──►  FastAPI control API (REST + WebSocket)    │
│                                                          │                                 │
│                     ┌────────────────────────────────────┼───────────────────────────┐     │
│                     ▼                                    ▼                           ▼     │
│           generator (asyncio, httpx)              collector (pollers)          report engine│
│           N virtual users, archetypes             /metrics, exporter,          knee, p95,   │
│           JWT sessions, SSE parsing               Mongo, psutil/docker         attribution  │
└─────────────────────────────┬──────────────────────────────────────────────────────────────┘
                              │ HTTP + SSE
                    ┌─────────▼─────────┐        ┌──────────────────────┐
                    │ LibreChat API     │───────►│ stub LLM (own port)  │  optional: real provider
                    │ :3080             │        │ OpenAI-compatible SSE│
                    └─────────┬─────────┘        └──────────────────────┘
                              │
                 Mongo · Meili · pgvector · RAG API
```

Why Python for everything: already installed on the target, best SSE/multipart ergonomics, one
runtime to service on Windows, and `httpx` + `asyncio` handles hundreds of open streams. Node is not
required (no frontend build step). Container image optional for Linux dev.

### 3.1 Generator

- **User seeding**: create N test users directly in Mongo (bcrypt hash + role) or via
  `/api/auth/register` with `REGISTER_MAX` raised for the test profile. Login once per VU, reuse JWT,
  refresh on 401. Never register in the hot loop.
- **Archetypes** (the "what each user is doing" editor), each a weighted action sequence:

| Archetype | Sequence | Exercises |
|---|---|---|
| `chatty` | new convo → POST chat → SSE → 1–3 follow-ups | event loop, Mongo writes, stream fan-out |
| `reader` | list convos → open convo → paginate messages → search | Mongo reads, Meili |
| `rag` | upload file → poll until `embedded` → chat with `file_search` | RAG API, pgvector, upload limiters |
| `power` | long prompt, branch/fork, regenerate, edit-and-resend | fork limiters, branching, history growth |
| `impatient` | start chat → abort after first token | abort path, pending-request cleanup |
| `auth` | login/logout/refresh cycle | auth + session limiters |

- **Traffic model**: open (arrival rate) with optional closed cap; ramp = steps or linear;
  think-time distribution; per-VU concurrency 1 (matching `LIMIT_CONCURRENT_MESSAGES`), with an
  explicit "multi-stream" toggle to *test the limiter* rather than fight it.
- **Per-request instrumentation**: scheduled-at vs sent-at vs first-byte vs first-token vs last-byte
  (coordinated-omission aware), status, limiter type, error class, bytes, token count when present,
  retry count, conversation id, message id.
- **Rate-limit profiles**: `respect` (stay under limits, measure real capacity) vs `probe` (keep the
  limiters in play to measure their behaviour and user-visible 429 rate). Optionally send
  `X-Forwarded-For` per VU when the deployment trusts a proxy — documented as requiring
  `TRUST_PROXY`/gateway config.

### 3.2 Stub LLM (`llm_stub`)

OpenAI-compatible `/v1/chat/completions` SSE with knobs: `ttft_ms`, `tokens_per_sec`, `max_tokens`,
`first_token_jitter`, and fault injection (`500`, `429`, mid-stream disconnect, slow trickle).
Purpose: measure LibreChat/Mongo/RAG capacity **without the 37 tok/s GPU ceiling**, then rerun the
identical scenario against `local27b` for end-to-end numbers. Reuses the reply-marker idea from
`e2e/setup/fake-model.js`.

### 3.3 Collector

Pollers on a fixed tick, written into the run's sample series:
API `/metrics` (bearer `METRICS_SECRET`), `virtUOS/librechat_exporter` if present, Mongo aggregates
(error messages, transactions, files, conversations), host CPU/RAM/disk (`psutil`) — prod has no
containers, so each node gets a tiny read-only collector agent (or node_exporter where present) —
optional OTLP span timings.

### 3.4 Dashboard (the "pick users and behaviour" surface)

- **Run launcher**: target URL, model path (stub/real), total users, ramp, duration, think time,
  rate-limit profile.
- **Mix editor**: archetype cards with % of users + per-archetype params (prompt size, follow-up
  count, file size/type, abort delay). Live "N users = X chatty / Y reader / Z rag".
- **Live view**: RPS, open streams, TTFT/ITL/total p50/p95/p99 per archetype, error rate,
  **errors by class and by limiter**, host/container CPU-RAM, Mongo write latency.
- **Run report**: capacity at p95 SLO, first-429 onset, throughput ceiling, bottleneck attribution
  (LLM vs Node CPU vs Mongo vs RAG vs limiter vs network), reconciliation table
  (client-observed errors vs `messages.error` vs metrics 4xx/5xx), export JSON + Markdown.

## 4. Milestones

| # | Deliverable | Done when |
|---|---|---|
| M0 | Skeleton: config schema, user seeding + login, one `chatty` action, error taxonomy module | 1 user completes a real chat; errors classified |
| M1 | VU pool + archetype mix + ramp; collector (API metrics + Mongo); minimal live dashboard; **provision/purge with residue verification** | 10–50 users ramp, live numbers, error table, run leaves zero residue |
| M2 | Stub LLM + `rag` archetype (upload → embedded → file_search with token proof) + **silent-failure probes** (Meili index lag, MCP tool probe, RAG query proof) + limit profiles | app-tier capacity measured with GPU removed; silent failures surfaced |
| M3 | Full reconciliation + report export + saturation-knee detection | one exported capacity report with attributed bottleneck |
| M4 | Windows packaging (service/Task Scheduler, config file, logs), docs, recorded baseline | runs unattended on the target box |

## 5. Provisioning, cleanup, and "no trace"

**Do not rely on LibreChat's own user deletion** — `deleteUserById`
(`packages/data-schemas/src/methods/user.ts:642`) removes only the user doc and `ToolApprovalGrant`,
and the admin API even ships `router.delete('/:id', …)` commented out. Account deletion is not a
cascade, so a leftover test user would leave conversations, messages, files, vectors and search docs
behind.

Chosen approach: **tagged test namespace + harness-owned purge + residue verification.**

1. **Provision** by direct Mongo insert of a *clone of a template user* (same role, balance, settings),
   with `email = lt+<runId>+<n>@loadlab.invalid`, a shared per-run bcrypt hash, and a `loadlab` run tag.
   Fast (no register limiter, no email), and identical to a real account in every field that matters
   to the app. Alternative (only if you prefer app-created accounts) is `/api/auth/register` with
   `REGISTER_MAX` raised for the test profile.
2. **Purge**, run-tagged and idempotent, in this order:
   - Meili: delete `messages`/`conversations` docs for the run's user ids (direct Meili delete-by-filter
     or the plugin's delete hooks), then trigger a reindex consistency check.
   - pgvector: `DELETE /documents` on rag_api per file id (removes vectors), then assert chunk rows gone.
   - Disk: `uploads/<user>` trees and any code-interpreter artifacts.
   - Mongo: `files`, `messages`, `conversations`, `transactions`, `balances`, `presets`, test-created
     `agents`/agent API keys, `sharedlinks`, `toolcalls`, sessions/Keyv keys, then `users` last.
   - Cache: clear the run's pending-request keys and rate-limit buckets.
3. **Verify zero residue**: assert zero Mongo counts for the run's ids, zero Meili hits for the marker
   token, zero pgvector rows for the run's file ids, zero files on disk, zero rows for `lt+<runId>` in
   any collection. The run is not "clean" until this passes; the report records the residue check.

### 5.1 Clean strategy with no containers

Production runs **native Windows services, no containers**, so isolation is done in **data**, not in a
sandbox. Every object a run creates is named by a run id and recorded in a **run ledger**; nothing is
ever deleted by guesswork.

**Run ledger (the core of the strategy)**
- One document in Mongo `loadlab_runs` + a local JSON copy on the loadlab box: `runId`, start/end,
  target URLs, config, every `userId`, every `conversationId`, every `fileId`, every `messageId`
  created, every Meili doc id, every pgvector `file_id`, every Keyv/session key touched.
- Written before the object is used, append-only. Purge reads the ledger — never a prefix scan — so a
  bug can never widen the blast radius.
- Crash safety: a run left `RUNNING` on startup triggers `purge --run <id> --verify` before anything
  else. `purge --all-stale` handles crashed loadlab itself.

**Provision**
- Clone one *template user* document from Mongo into N test users:
  `email = lt+<runId>-<n>@loadlab.invalid`, `name = loadlab <runId> <n>`, shared per-run bcrypt hash,
  same role/balance/settings as the template. Insert, don't register: no limiter, no email, no admin
  panel churn, and passwords hash exactly like production users.
- Test users are the only prod-visible artifact while a run is live (they show in the admin user list).
  The email domain makes them obviously synthetic; the window is the run duration plus purge time.

**Purge (ordered, idempotent, manifest-scoped)**
1. Meili: delete `messages` / `conversations` docs for the ledger's user ids and doc ids (Meili API on
   B, or the model's delete hooks); then re-check that the marker token has zero hits.
2. pgvector: `DELETE /documents` on rag_api for each ledger `file_id`; then SQL count = 0.
3. Disk: `uploads/<userId>` trees on A; code-interpreter/artifact files if any.
4. Mongo: `files`, `messages`, `conversations`, `transactions`, `balances`, `presets`, run-created
   `agents` + agent API keys, `sharedlinks`, `toolcalls`, `sessions`, then `users` last.
5. Cache: pending-request keys and limiter buckets for the run's user ids/IPs.
6. **Verify zero residue** and write the result into the report: Mongo counts 0, Meili hits 0,
   pgvector rows 0, files on disk 0, no `lt+<runId>` string anywhere.

**Guard rails**
- `--dry-run` prints the exact delete plan (collection, filter, count) and deletes nothing.
- Refuse to delete anything that does not carry the run id; refuse if the manifest is missing;
  hard cap on documents per run (configurable) as a runaway brake.
- Snapshot every document to `runs/<id>/deleted.jsonl` **before** deletion, so a mistaken purge is
  recoverable from the ledger rather than from a database backup.
- Purge runs at the *end* of a run and again at the *start* of the next one (belt and braces).

### 5.2 Test-profile `.env` without containers

No containers means applying the profile is an **edit + service restart**, so it is treated as a
change-managed step, not an afterthought:

1. `loadlab env apply --profile test` — backs up `.env` to `.env.loadlab.bak-<ts>`, writes the test
   values (`METRICS_SECRET`, raised `MESSAGE_*`/`FILE_UPLOAD_*`/`LOGIN_*`/`REGISTER_*`, `SEARCH=true`
   unchanged, `LIMIT_CONCURRENT_MESSAGES=true` kept), records a SHA-256 of the result.
2. Restarts the LibreChat service (NSSM / `Restart-Service` / systemd) and waits for `/api/config` 200
   plus a `/metrics` 200 with the bearer token.
3. `loadlab env restore` at the end — restores the backup byte-for-byte, verifies the SHA-256 matches
   the pre-run hash, restarts, re-checks health. A watchdog timer restores automatically if loadlab
   dies mid-window.
4. The report records both hashes, so "prod config is unchanged" is provable, not asserted.

If a restart window is unacceptable, the fallback is `probe` mode: leave limits as they are, accept
429s as the measured result, and cap throughput at the current per-IP ceiling.

### 5.3 Sandbox on the dev box (containers), never in prod

The dev box already runs Docker, so destructive or exploratory scenarios use a **second throwaway
compose stack** there (own Mongo, Meili, pgvector, LibreChat) — not in production. Prod only ever gets
the manifest-scoped, residue-verified mode from 5.1. This keeps the expensive/reckless tests on the dev
machine while still validating the exact same loadlab build that ships to Windows.

## 6. Production topology mapping

Confirmed layout: **(A)** LibreChat API + admin panel + rag_api · **(B)** Meilisearch + MongoDB ·
**(C)** PostgreSQL/pgvector.

| Concern | Where it lands |
|---|---|
| Generator | separate 4th box (never on A/B/C) → HTTPS to A |
| HTTP/SSE + MCP probes | A (port 3080/443) |
| Meili index lag + failed tasks, Mongo seeds/purge | B (7700, 27017) — read/write, test-user scoped |
| Vector residue + chunk counts | C (5432) read-only, writes only via rag_api `DELETE /documents` |
| rag_api health/latency | A:8000 internal; seen through upload + `file_search` |
| Per-node saturation | host CPU/RAM/disk per box (node_exporter or a small collector agent) |

Topology consequences to design for:
- A hosts **both** the API and rag_api → RAG-heavy archetypes compete with chat for the same CPU. The
  mix editor must be able to isolate them (`rag_only`, `chat_only`, `mixed`) to attribute correctly.
- Meili on B has its own ceiling (indexing + search) that a green API `/metrics` will not show — hence
  the index-lag probe is mandatory, not optional.
- Mongo on B receives the write amplification of every chat turn; watch `mongoose_query_duration_seconds`
  on A alongside B's disk/IO.
- Cross-server latency (A→B, A→C) is part of the measurement; the report keeps per-hop timings.

## 7. Risks / decisions to settle

1. **Per-IP limiters** — decided: test-profile `.env` applied and restored by `loadlab env`, with
   SHA-256 proof and an auto-restore watchdog (5.2). `probe` mode is the no-restart fallback.
2. **User provisioning** — decided: direct Mongo insert of template-user clones, tracked in the run
   ledger (5.1). SSO-only prod would instead reuse pre-issued sessions.
3. **Redis** — still open: production multi-node behaviour (concurrency limiter, resumable streams)
   is not exercised by the single-node dev box.
4. **Generator placement** — decided: run on a separate machine; dev shares the box, so its p95 is a floor.
5. **Secrets** — decided: `METRICS_SECRET` stays in the loadlab config/env, never in the UI.
