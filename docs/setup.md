# loadlab — setup guide

Two supported ways to run the harness. Both run the **same Python package**; pick by where the
LibreChat deployment lives.

| | container | Windows venv (production target) |
|---|---|---|
| host | Linux/macOS/Windows + Docker | Windows Server, native, no containers |
| install | `docker compose up -d --build` | `py -3.12 -m venv` + `pip install -r requirements.txt` |
| service | restart policy | NSSM or Task Scheduler |
| config | `.env` next to compose | `%ProgramData%\loadlab\loadlab.env` |
| best for | dev box, quick trials | capacity runs against the real topology |

---

## 0. What loadlab needs from the LibreChat side

No LibreChat code changes. Three things on the deployment:

```
1. credentials/access   Mongo read-write (seed + purge test users)
                        Meili API key (index-lag probe + purge)
                        pgvector DSN (residue verification)
                        uploads dir (read-write, for purge)
                        LibreChat HTTP(S) reachable
2. test .env profile    raised limiters + METRICS_SECRET (else 40 msg/min per IP)
                        applied/restored with: loadlab env apply | restore
3. optional             a custom endpoint pointing at the stub (GPU-free capacity runs)
                        local HF embeddings for rag_api (no OpenAI key)
```

Without (2) the harness still runs, but every test measures the per-IP limiter. Without (3) point the
scenario at a real endpoint/model.

Stub endpoint in `librechat.yaml` (needed only for GPU-free runs):

```yaml
endpoints:
  custom:
    - name: 'loadlabstub'
      apiKey: 'sk-noauth-stub'
      baseURL: 'http://host.docker.internal:8099/v1'   # container
      # baseURL: 'http://127.0.0.1:8099/v1'            # Windows, LibreChat on the same host
      models: { default: ['loadlab-stub'], fetch: false }
      titleConvo: false
```

---

## 1. Container (dev box)

Prereqs: Docker Engine/Desktop with compose v2.

```bash
cd ~/dev/loadlab
cp .env.example .env          # edit URLs/keys for your LibreChat stack
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8090/api/health
```

- Dashboard: `http://127.0.0.1:8090/`
- Stub LLM: `http://127.0.0.1:8099/v1` (OpenAI-compatible)
- Runs/reports persist in `./runs` (bind mount).
- `host.docker.internal` is provided via `extra_hosts` (Linux too).
- Purge deletes uploaded files through the bind mount: set `LIBRECHAT_UPLOADS_DIR`
  (default `../librechat/uploads`) and `LOADLAB_UPLOADS_DIR=/librechat-uploads`.

Verify a run:

```bash
curl -s -X POST http://127.0.0.1:8090/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"name":"smoke","users":2,"ramp_s":1,"duration_s":10,
       "mix":{"chatty":{"weight":1,"think_ms":200}},
       "target":{"endpoint":"loadlabstub","model":"loadlab-stub"}}'
sleep 12
curl -s http://127.0.0.1:8090/api/runs | head
```

Stop / clean:

```bash
docker compose down
docker compose run --rm loadlab python -m loadlab.cli purge --run <run-id> --apply
```

Notes:
- The container runs as UID 10001. If `./runs` or the uploads mount is not writable, `chown` it.
- To scrape LibreChat `/metrics`, set `LOADLAB_METRICS_SECRET`; to read Meili, `LOADLAB_MEILI_KEY`.
- For a gateway (LiteLLM/Azure) set `LOADLAB_PROVIDER_URL` / `LOADLAB_LLM_METRICS_URL` in `.env`.

### Reaching Mongo / Meili / pgvector from the container

LibreChat's HTTP port is usually published; its data services often are not. Two options:

**A. Published ports or LAN names** — publish `27017`, `7700`, `5432` on the LibreChat host (dev), or
use the real hostnames (prod). Then keep the `host.docker.internal` / hostname URLs in `.env`.

**B. Join the LibreChat compose network** (dev, no port publishing):

```bash
docker network ls | grep librechat          # e.g. librechat_default
```

```yaml
# docker-compose.override.yml (next to loadlab's compose)
services:
  loadlab:
    networks: [librechat]
    environment:
      - LOADLAB_MONGO_URI=mongodb://chat-mongodb:27017/LibreChat
      - LOADLAB_MEILI_URL=http://chat-meilisearch:7700
      - LOADLAB_RAG_URL=http://rag_api:8000
      - LOADLAB_PG_DSN=postgresql://myuser:mypassword@vectordb:5432/mydatabase
  stub:
    networks: [librechat]

networks:
  librechat:
    external: true
    name: librechat_default
```

Verify from inside the container:

```bash
docker exec loadlab python -u -c "import socket;[print(h,p,socket.create_connection((h,p),2) and 'ok') for h,p in [('chat-mongodb',27017),('chat-meilisearch',7700),('vectordb',5432)]]"
```

---

## 2. Windows venv (native, no containers)

Prereqs: Python 3.12 x64, network access to LibreChat/Mongo/Meili/Postgres, a dedicated service account
or your own login.

```powershell
# 1. code + venv
mkdir C:\loadlab
# copy the loadlab/ directory here (git clone or zip)
cd C:\loadlab
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. config
mkdir "$env:ProgramData\loadlab"
copy .env.example "$env:ProgramData\loadlab\loadlab.env"
notepad "$env:ProgramData\loadlab\loadlab.env"
#   LOADLAB_BASE_URL=https://librechat.internal
#   LOADLAB_MONGO_URI=mongodb://mongo-host:27017/LibreChat
#   LOADLAB_MEILI_URL=http://meili-host:7700
#   LOADLAB_UPLOADS_DIR=C:\librechat\uploads
#   LOADLAB_PG_DSN=postgresql://user:pass@pg-host:5432/mydatabase
#   LOADLAB_RESTART_CMD=powershell -c "Restart-Service LibreChat"
$env:LOADLAB_ENV_FILE="$env:ProgramData\loadlab\loadlab.env"
```

Run in the foreground (first check):

```powershell
cd C:\loadlab
.\.venv\Scripts\python.exe -m loadlab.cli serve --host 0.0.0.0 --port 8090
# dashboard: http://<server>:8090/    stub (optional): --port 8099
```

Health + a smoke run:

```powershell
Invoke-RestMethod http://localhost:8090/api/health
Invoke-RestMethod -Method Post http://localhost:8090/api/runs -ContentType application/json -Body @'
{"name":"win-smoke","users":2,"ramp_s":1,"duration_s":10,
 "mix":{"chatty":{"weight":1,"think_ms":200}},
 "target":{"endpoint":"loadlabstub","model":"loadlab-stub"}}
'@
```

### Install as a service (recommended)

NSSM:

```powershell
nssm install loadlab "C:\loadlab\.venv\Scripts\python.exe" "-m loadlab.cli serve --host 0.0.0.0 --port 8090"
nssm set loadlab AppDirectory C:\loadlab
nssm set loadlab AppEnvironmentExtra "LOADLAB_ENV_FILE=C:\ProgramData\loadlab\loadlab.env"
nssm set loadlab AppStdout C:\ProgramData\loadlab\loadlab.out.log
nssm set loadlab AppStderr C:\ProgramData\loadlab\loadlab.err.log
nssm set loadlab Start SERVICE_AUTO_START
nssm start loadlab
```

No NSSM? Task Scheduler → "Create Task" → run whether user is logged on or not → action
`C:\loadlab\.venv\Scripts\python.exe -m loadlab.cli serve --host 0.0.0.0 --port 8090`,
start in `C:\loadlab`, set the `LOADLAB_ENV_FILE` environment for the task.

Firewall:

```powershell
New-NetFirewallRule -DisplayName "loadlab" -Direction Inbound -Protocol TCP -LocalPort 8090,8099 -Action Allow
```

Restart after config/`env apply`:

```powershell
Restart-Service loadlab
```

### Apply / restore the test `.env` profile

```powershell
cd C:\loadlab
.\.venv\Scripts\python.exe -m loadlab.cli env apply --env-file C:\librechat\.env
# ... run the tests ...
.\.venv\Scripts\python.exe -m loadlab.cli env restore
```

`LOADLAB_RESTART_CMD` must restart the LibreChat service (the command above uses `Restart-Service`).
The command saves `.env.loadlab.bak`, writes the profile, restarts, and `restore` proves the bytes
match the pre-run SHA-256.

---

## 3. Optional: local HuggingFace embeddings for rag_api (no OpenAI key)

Only needed if your `rag_api` currently fails embeddings. On the Windows rag_api host:

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install langchain-huggingface sentence-transformers "pandas>=2.2.2"
$env:EMBEDDINGS_PROVIDER="huggingface"
$env:EMBEDDINGS_MODEL="sentence-transformers/all-MiniLM-L6-v2"
$env:HF_HOME="C:\rag-api\data\hf"
```

Pre-download the model once so the service starts offline, then verify `/embed` returns
`{"status": true}`. On the dev box this is `docker compose -f docker-compose.yml -f
docker-compose.override.yml -f docker-compose.embeddings.yml up -d rag_api`.

---

## 4. Checks and troubleshooting

| symptom | cause / fix |
|---|---|
| SSE `{"message":"Illegal request"}` | LibreChat requires a browser `User-Agent`; loadlab sends Chrome's — check a proxy stripped it |
| `/metrics` 401 | `METRICS_SECRET` unset or wrong; set it and restart LibreChat |
| logins 429 during a run | test profile not applied (`loadlab env apply`) |
| `provider_429` never appears | no gateway/Azure in front; Strata queues instead of 429ing |
| purge reports `rag_vectors: -1` | pg DSN missing/unreachable — install `psycopg[binary]` or set `LOADLAB_PG_DSN` |
| purge leaves vectors | rag_api scopes deletes per owner; agent files also need `entity_id` (handled when the ledger recorded it) |
| uploads not deleted | `LOADLAB_UPLOADS_DIR` wrong or not writable by the loadlab account |
| container can't reach LibreChat | use `host.docker.internal` (compose sets `extra_hosts`) or the host's LAN IP |
| container can't reach Mongo/Meili/pg | publish those ports, or join the LibreChat compose network (see §1) |

## 5. Recovery and partial state

Everything a run creates is recorded on disk under `runs/<run-id>/`:

```
ledger.json          users, files (+owner/entity), conversations, heartbeat, status
purge-manifest.json  ids expanded at purge time; merged on every retry
deleted.jsonl        snapshot of each document before deletion (forensic, not auto-restore)
report.md/.json      results
```

Guarantees:

- **Fail-closed**: users are deleted last, and only if vectors, Meili docs, uploads and Mongo rows all
  verify zero. If a dependency is down (e.g. rag_api), purge stops with `ok=false, retryable=true`
  and keeps the users so a retry can still authorise the remaining vectors.
- **Idempotent**: re-running the same `purge --run <id>` continues where it stopped; nothing is
  deleted by prefix scan.
- **Abandoned runs**: the ledger heartbeats every 30 s. `loadlab serve` sweeps runs whose heartbeat is
  older than `LOADLAB_STALE_GRACE_S` (default 900 s) on startup, and the same sweep is available as a
  scheduled command.
- **Test `.env`**: `env apply` writes a state file; `env guard` restores the backed-up bytes if the
  profile is older than a limit, so a crashed run cannot leave prod limits raised.

Schedule both (defence in depth — covers the case where loadlab never restarts):

```powershell
# Windows Task Scheduler, every 15 minutes
schtasks /Create /TN "loadlab sweep" /SC MINUTE /MO 15 /RU SYSTEM /TR ^
 "C:\loadlab\.venv\Scripts\python.exe -m loadlab.cli purge --stale --grace 900 --apply"
schtasks /Create /TN "loadlab env guard" /SC MINUTE /MO 15 /RU SYSTEM /TR ^
 "C:\loadlab\.venv\Scripts\python.exe -m loadlab.cli env guard --max-minutes 120"
```

```bash
# cron equivalent
*/15 * * * * cd /opt/loadlab && ./.venv/bin/python -m loadlab.cli purge --stale --grace 900 --apply
*/15 * * * * cd /opt/loadlab && ./.venv/bin/python -m loadlab.cli env guard --max-minutes 120
```

What is *not* guaranteed: there is no transaction across Mongo/Meili/pg/files (ordering + verification
instead), `deleted.jsonl` is not replayed automatically, and a lost `runs/<id>` directory means no
target for cleanup (deliberate: no scanning).

Final sanity pass, in order: `/api/health` → `/api/defaults` → a 2-user 10 s smoke run →
`loadlab purge --run <id>` (dry) → apply.
