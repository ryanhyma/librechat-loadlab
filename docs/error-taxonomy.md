# Error taxonomy — loadlab

What "a user error" means in LibreChat, where each class comes from, and how loadlab detects it.
Derived from the source (`api/server/middleware`, `packages/api/src/stream`, `agents/handlers.ts`)
and validated against the running stack.

## Transport errors (the wire tells you)

| class | trigger | detected at |
|---|---|---|
| `limiter_concurrent` | 429 `{type:"concurrent"}` — `LIMIT_CONCURRENT_MESSAGES` allows one in-flight message per user | chat POST |
| `limiter_message_ip` | 429 message limiter per IP (`MESSAGE_IP_MAX`, default 40/min) | chat POST |
| `limiter_message_user` | 429 message limiter per user (`MESSAGE_USER_MAX`) | chat POST |
| `limiter_upload` | 429 file-upload limiters (`FILE_UPLOAD_IP_MAX` 100/15min, `_USER_MAX` 50/15min) | `POST /api/files` |
| `limiter_login` / `limiter_register` | 429 auth limiters (`LOGIN_MAX`, `REGISTER_MAX`) | login / register |
| `limiter_tool` | 429 tool-call limiter (1 s window) | tool POST |
| `limiter_generic` | any other 429 (agent-event admission, MCP app, fork, share, TTS/STT, …) | any HTTP |
| `validation` | 400 / 422 — bad payload, unknown file id, invalid UUID | any HTTP |
| `auth` | 401 / 403 — JWT, ban, ACL, agent permission | any HTTP |
| `server` | 5xx — unhandled exception in the API | any HTTP |

Limiter bodies are parsed, not guessed: `type` (e.g. `concurrent`), `limiter` (`ip`/`user`),
`max`, `windowInMinutes`, `limit`, `pendingRequests`.

## Stream errors (HTTP 200, failure inside the stream)

| class | trigger | detected at |
|---|---|---|
| `sse_error` | `event: error` frame (`handleError`), data string or `{message}` | SSE |
| `stream_terminal_error` | `final{terminalStatus:"error"}` or `responseMessage.error` | SSE `final` |
| `stream_reconcile` | `final.reconcile` with `generation_replaced` / `abort_persistence_failed` / `terminal_payload_missing` | SSE `final` |
| `abort` | `final{terminalStatus:"aborted"}` / `aborted` / `earlyAbort` | SSE `final` |
| `stream_stall` | no SSE data for the idle window (20 s stub, 45 s real model) | SSE reader |
| `stream_disconnect` | read error, timeout, or stream closed with no content — LibreChat's own UI turns this into `CONNECTION_ERROR_TEXT` | client |

## Silent failures (a green status code, a broken user)

| class | what actually happens | detection |
|---|---|---|
| `mcp_tool_error` | tool failure becomes tool *output* text `Error: [<tool>] tool call failed: …`; the model may summarise it away | SSE tool step + tool output text |
| `silent_meili` | Meili indexing is detached (`runDetachedMeiliOperation`); failures are logged, search silently returns fewer hits | marker message created, then `GET /api/messages?search=<marker>` polled for 15 s |
| `silent_rag` | `rag_api /embed` returns **200** with `status:false`; `file_search` swallows HTTP errors into “No results found or errors occurred…” | direct `/embed` + `/query` probe, and agent answer must contain the fixture codename |
| `probe_error` | the probe itself could not reach its target (network/timeout) | probe |

## Harness errors (never a virtual user crash)

| class | meaning |
|---|---|
| `unknown` | unexpected exception inside a virtual user; recorded and the VU continues |

## Verification layers (used to reconcile)

| layer | evidence |
|---|---|
| client | every HTTP status/body and every SSE event of the run |
| server metrics | `http_requests_total{status}`, `sse_streams_total{status}`, `upload_*`, `generation_stream_recoveries_total` |
| database | `messages.error`, `transactions` (rejected requests), `files.embedded`, agent `tool_resources` |
| probes | Meili index lag, rag_api embed/query, agent file_search answer content |

A run is only “green” when all four agree. Mismatches are reported as findings:
client error but no DB error row (swallowed), DB error row but no client error (silent), or
HTTP 200s while probes fail (green dashboard, broken feature).

## Readiness / severity in reports

- **Blocking** — `auth`, `limiter_concurrent` (when unexpected), `server`, `stream_terminal_error`,
  `silent_rag`, `agent_file_search` failure.
- **Capacity signal** — `limiter_*` at high concurrency, `stream_stall`, `stream_disconnect`.
- **Expected** — `abort` (the `impatient` archetype exercises it), `validation` in negative tests.
