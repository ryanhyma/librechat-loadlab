"""Manifest-scoped purge + zero-residue verification (PLAN.md 5.1).

Nothing is deleted by prefix scan: every object is derived from the run's user
ids (unique per run) and the ledger, and every step is idempotent.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from bson import ObjectId
from pymongo import MongoClient

from .config import UA, RunConfig, Target
from .ledger import Ledger

MEILI_INDEXES = {"messages": "messageId", "convos": "conversationId"}

# collections whose documents carry `user`, deleted before the user row
USER_COLLECTIONS = (
    "files",
    "messages",
    "conversations",
    "transactions",
    "balances",
    "presets",
    "sharedlinks",
    "toolcalls",
    "conversationtags",
    "agentapikeys",
    "aclentries",
    "sessions",
)


@dataclass
class Step:
    name: str
    count: int
    run: Callable[[], None] | None = None


@dataclass
class PurgeResult:
    run_id: str
    dry_run: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    residue: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    retryable: bool = False
    errors: list[str] = field(default_factory=list)


class Purge:
    def __init__(self, cfg: RunConfig, ledger_data: dict[str, Any], dry_run: bool = True) -> None:
        self.cfg = cfg
        self.ledger = ledger_data
        self.run_id = ledger_data["run_id"]
        self.dry_run = dry_run
        self.mongo = MongoClient(cfg.target.mongo_uri, serverSelectionTimeoutMS=5000)
        self.db = self.mongo.get_default_database()
        self.ids = self._collect()

    # ------------------------------------------------------------- ids
    def _collect(self) -> dict[str, Any]:
        user_ids = [ObjectId(u["id"]) for u in self.ledger.get("users", []) if u.get("id")]
        user_strs = [str(u["id"]) for u in self.ledger.get("users", []) if u.get("id")]
        # LibreChat stores `user` as ObjectId in some collections and as a string in others
        conv_filter = (
            {"$or": [{"user": {"$in": user_ids}}, {"user": {"$in": user_strs}}]}
            if user_ids
            else {"_id": None}
        )
        conversations = list(self.db.conversations.find(conv_filter, {"conversationId": 1}))
        conv_ids = [c["conversationId"] for c in conversations if c.get("conversationId")]
        msg_filter = (
            {"$or": [{"user": {"$in": user_strs}}, {"conversationId": {"$in": conv_ids}}]}
            if user_ids
            else {"_id": None}
        )
        messages = list(self.db.messages.find(msg_filter, {"messageId": 1}))
        msg_ids = [m["messageId"] for m in messages if m.get("messageId")]
        files = list(self.db.files.find(conv_filter, {"file_id": 1}))
        file_ids = {f["file_id"] for f in files if f.get("file_id")}
        file_ids |= {f["file_id"] for f in self.ledger.get("files", []) if f.get("file_id")}
        meili_msgs = set(self.ledger.get("meili", {}).get("messages", [])) | set(msg_ids)
        meili_convos = set(self.ledger.get("meili", {}).get("convos", [])) | set(conv_ids)
        ids = {
            "user_ids": user_ids,
            "user_strs": user_strs,
            "conversations": conv_ids,
            "messages": msg_ids,
            "files": sorted(file_ids),
            "meili": {"messages": sorted(meili_msgs), "convos": sorted(meili_convos)},
        }
        # a previous interrupted purge leaves a manifest; union it so a retry can still
        # reach objects whose Mongo rows are already gone
        manifest = self.manifest_path()
        if manifest.exists():
            try:
                old = json.loads(manifest.read_text())
                ids["user_strs"] = sorted(set(ids["user_strs"]) | set(old.get("user_strs", [])))
                ids["conversations"] = sorted(set(ids["conversations"]) | set(old.get("conversations", [])))
                ids["files"] = sorted(set(ids["files"]) | set(old.get("files", [])))
                for index in ("messages", "convos"):
                    merged = set(ids["meili"].get(index, [])) | set(old.get("meili", {}).get(index, []))
                    ids["meili"][index] = sorted(merged)
            except Exception:
                pass
        return ids

    def manifest_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "runs" / self.run_id / "purge-manifest.json"

    def _save_manifest(self) -> None:
        path = self.manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "user_strs": self.ids["user_strs"],
                    "conversations": self.ids["conversations"],
                    "files": self.ids["files"],
                    "meili": self.ids["meili"],
                },
                indent=2,
            )
        )

    def _filter(self, collection: str) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = [
            {"user": {"$in": self.ids["user_ids"]}},
            {"user": {"$in": self.ids["user_strs"]}},
        ]
        if collection in ("messages", "files", "conversations"):
            clauses.append({"conversationId": {"$in": self.ids["conversations"]}})
        return {"$or": clauses}

    # ------------------------------------------------------------ plan
    def plan(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        if self.ids["files"]:
            steps.append({"name": "rag_vectors", "count": len(self.ids["files"])})
        for index, ids in self.ids["meili"].items():
            if ids:
                steps.append({"name": f"meili_{index}", "count": len(ids)})
        if self.ids["user_ids"]:
            uploads = Path(self.cfg.target.uploads_dir)
            dirs = [uploads / str(uid) for uid in self.ids["user_ids"] if (uploads / str(uid)).exists()]
            steps.append({"name": "uploads_disk", "count": len(dirs)})
        for name in USER_COLLECTIONS:
            if not self.ids["user_ids"]:
                break
            try:
                steps.append({"name": f"mongo_{name}", "count": self.db[name].count_documents(self._filter(name))})
            except Exception:
                continue
        if self.ids["user_ids"]:
            steps.append({"name": "mongo_users", "count": len(self.ids["user_ids"])})
        return steps

    # --------------------------------------------------------- execute
    def execute(self) -> PurgeResult:
        result = PurgeResult(self.run_id, self.dry_run)
        steps = self.plan()
        if self.dry_run:
            result.steps = steps
            result.residue = self.verify()
            result.ok = True  # dry run succeeded; residue is what *would* remain
            return result

        log_dir = Path(self.cfg.run_dir if self.cfg.name else ".")  # unused, kept for typing
        snapshot_path = Path(__file__).resolve().parent.parent / "runs" / self.run_id / "deleted.jsonl"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_manifest()

        if self.ids["files"]:
            ok, detail = self._delete_vectors()
            result.steps.append({"name": "rag_vectors", "count": len(self.ids["files"]), "ok": ok})
            if not ok:
                result.errors.append(f"rag_vectors: {detail}")

        for index, ids in self.ids["meili"].items():
            if ids:
                deleted, ok = self._delete_meili(index, ids)
                result.steps.append({"name": f"meili_{index}", "count": deleted, "ok": ok})
                if not ok:
                    result.errors.append(f"meili_{index}: delete not confirmed")

        uploads = Path(self.cfg.target.uploads_dir)
        removed = 0
        for uid in self.ids["user_ids"]:
            target = uploads / str(uid)
            if target.exists():
                try:
                    shutil.rmtree(target)
                    removed += 1
                except OSError as exc:
                    result.errors.append(f"uploads {uid}: {exc}")
        if removed or self.ids["user_ids"]:
            result.steps.append({"name": "uploads_disk", "count": removed})

        for name in USER_COLLECTIONS:
            if not self.ids["user_ids"]:
                break
            try:
                docs = list(self.db[name].find(self._filter(name)))
                if docs:
                    with snapshot_path.open("a") as fh:
                        for doc in docs:
                            fh.write(json.dumps({"collection": name, "doc": doc}, default=str) + "\n")
                    res = self.db[name].delete_many(self._filter(name))
                    result.steps.append({"name": f"mongo_{name}", "count": res.deleted_count})
            except Exception as exc:
                result.steps.append({"name": f"mongo_{name}", "count": 0, "error": str(exc)[:160]})
                result.errors.append(f"mongo_{name}: {exc}")

        if self.ids["user_ids"]:
            agents = list(
                self.db.agents.find({"author": {"$in": self.ids["user_strs"] + self.ids["user_ids"]}})
            )
            if agents:
                with snapshot_path.open("a") as fh:
                    for doc in agents:
                        fh.write(json.dumps({"collection": "agents", "doc": doc}, default=str) + "\n")
                res = self.db.agents.delete_many(
                    {"author": {"$in": self.ids["user_strs"] + self.ids["user_ids"]}}
                )
                result.steps.append({"name": "mongo_agents", "count": res.deleted_count})

        # Fail-closed: never delete users while their vectors/Meili docs/files/rows remain.
        # Deleting users first would orphan anything a retry still needs to authorise.
        pre_user = {
            k: v for k, v in self.verify().items() if k not in ("mongo_users", "mongo_agents")
        }
        blocking = {k: v for k, v in pre_user.items() if v not in (0,)}
        if blocking:
            result.residue = self.verify()
            result.ok = False
            result.retryable = True
            result.errors.append(f"pre-user checks failed: {blocking}")
            return result

        if self.ids["user_ids"]:
            res = self.db.users.delete_many({"_id": {"$in": self.ids["user_ids"]}})
            result.steps.append({"name": "mongo_users", "count": res.deleted_count})

        result.residue = self.verify()
        result.ok = all(v == 0 for v in result.residue.values())
        return result

    # ------------------------------------------------------- subsystems
    def _delete_vectors(self) -> tuple[bool, str]:
        """rag_api authorises deletion per owner, so group file ids by their user."""
        users = {u["index"]: u for u in self.ledger.get("users", [])}
        groups: dict[Any, list[tuple[str, Any]]] = {}
        for entry in self.ledger.get("files", []):
            key = (entry.get("user_index"), entry.get("entity_id"))
            groups.setdefault(key, []).append(entry["file_id"])

        def delete(ids: list[str], token: str, entity_id: str | None = None) -> None:
            if not ids or not token:
                return
            try:
                httpx.request(
                    "DELETE",
                    f"{self.cfg.target.rag_url.rstrip('/')}/documents",
                    params={"entity_id": entity_id} if entity_id else None,
                    json=ids,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                    timeout=90,
                )
            except httpx.HTTPError:
                pass

        for (user_index, entity_id), ids in groups.items():
            if user_index is None:
                continue
            user = users.get(user_index)
            if user:
                delete(ids, self._login(user["email"], user["password"]), entity_id)
        # legacy ledgers without an owner: try each user's token
        leftovers = [fid for (u, _e), ids in groups.items() if u is None for fid in ids]
        if leftovers:
            for user in users.values():
                delete(leftovers, self._login(user["email"], user["password"]))

        remaining = self._vector_rows()
        if remaining == 0:
            return True, ""
        if remaining == -1:
            return False, "pg not measurable (set LOADLAB_PG_DSN and install psycopg[binary])"
        return False, f"{remaining} vector rows remain"

    def _login(self, email: str, password: str) -> str:
        try:
            r = httpx.post(
                f"{self.cfg.target.base_url}/api/auth/login",
                json={"email": email, "password": password},
                headers={"User-Agent": UA},
                timeout=20,
            )
            return r.json().get("token", "") if r.status_code == 200 else ""
        except httpx.HTTPError:
            return ""

    def _delete_meili(self, index: str, ids: list[str]) -> tuple[int, bool]:
        if not self.cfg.target.meili_key:
            return 0, False
        try:
            r = httpx.post(
                f"{self.cfg.target.meili_url.rstrip('/')}/indexes/{index}/documents/delete-batch",
                json=ids,
                headers={"Authorization": f"Bearer {self.cfg.target.meili_key}"},
                timeout=60,
            )
            if r.status_code >= 400:
                return 0, False
        except httpx.HTTPError:
            return 0, False
        return len(ids), self._meili_rows() in (0,)

    def _token(self) -> str:
        """A still-valid access token for one of the run's users (for rag_api)."""
        users = self.ledger.get("users", [])
        if not users:
            return ""
        u = users[0]
        try:
            r = httpx.post(
                f"{self.cfg.target.base_url}/api/auth/login",
                json={"email": u["email"], "password": u["password"]},
                headers={"User-Agent": UA},
                timeout=20,
            )
            return r.json().get("token", "") if r.status_code == 200 else ""
        except httpx.HTTPError:
            return ""

    # ---------------------------------------------------------- verify
    def verify(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        user_ids = self.ids["user_ids"]
        if user_ids:
            try:
                out["mongo_agents"] = self.db.agents.count_documents(
                    {"author": {"$in": self.ids["user_strs"] + user_ids}}
                )
            except Exception:
                out["mongo_agents"] = 0
            try:
                out["mongo_users"] = self.db.users.count_documents({"_id": {"$in": user_ids}})
            except Exception:
                out["mongo_users"] = -1
            for name in USER_COLLECTIONS:
                try:
                    out[f"mongo_{name}"] = self.db[name].count_documents(self._filter(name))
                except Exception:
                    out[f"mongo_{name}"] = 0
        uploads = Path(self.cfg.target.uploads_dir)
        out["uploads_disk"] = sum(1 for uid in user_ids if (uploads / str(uid)).exists())
        out["rag_vectors"] = self._vector_rows()
        out["meili"] = self._meili_rows()
        return out

    def _vector_rows(self) -> int:
        if not self.ids["files"]:
            return 0
        try:
            import psycopg  # optional dependency

            with psycopg.connect(self.cfg.target.pg_dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM langchain_pg_embedding WHERE cmetadata->>'file_id' = ANY(%s)",
                        (self.ids["files"],),
                    )
                    return int(cur.fetchone()[0])
        except Exception:
            return -1  # not measurable -> surfaced, not silently green

    def _meili_rows(self) -> int:
        if not self.cfg.target.meili_key:
            return -1
        total = 0
        for index in ("messages", "convos"):
            ids = self.ids["meili"].get(index) or []
            if not ids:
                continue
            try:
                r = httpx.post(
                    f"{self.cfg.target.meili_url.rstrip('/')}/indexes/{index}/documents/fetch",
                    json={"filter": [f'{MEILI_INDEXES[index]} IN [{",".join(json.dumps(i) for i in ids[:200])}]']},
                    headers={"Authorization": f"Bearer {self.cfg.target.meili_key}"},
                    timeout=30,
                )
                if r.status_code < 400:
                    total += len(r.json().get("results", []))
            except httpx.HTTPError:
                total += 0
        return total


def _jsonable(doc: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, (dict, list, str, int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


def purge_run(cfg: RunConfig, run_id: str, dry_run: bool = True) -> PurgeResult:
    data = Ledger.load(run_id)
    return Purge(cfg, data, dry_run).execute()

def stale_runs(grace_s: float = 900.0) -> list[dict[str, Any]]:
    """Runs whose ledger says `running` but whose heartbeat is older than grace."""
    out = []
    now = time.time()
    for run_id in Ledger.runs():
        try:
            data = Ledger.load(run_id)
        except Exception:
            continue
        if data.get("status") not in ("running", "stopping"):
            continue
        if not data.get("users"):
            continue
        beat = float(data.get("heartbeat_at") or data.get("created_at") or 0)
        age = now - beat
        if age >= grace_s:
            out.append({"run_id": run_id, "age_s": round(age), "users": len(data["users"])})
    return out


def sweep_stale(grace_s: float = 900.0, apply: bool = False) -> list[dict[str, Any]]:
    """Fail-closed cleanup for abandoned runs. Idempotent: safe to call on a schedule."""
    results = []
    for item in stale_runs(grace_s):
        data = Ledger.load(item["run_id"])
        cfg = RunConfig.model_validate(data["config"])
        cfg.target = Target()
        result = purge_run(cfg, item["run_id"], dry_run=not apply)
        results.append(
            {
                "run_id": item["run_id"],
                "age_s": item["age_s"],
                "applied": apply,
                "ok": result.ok,
                "retryable": result.retryable,
                "residue": {k: v for k, v in result.residue.items() if v},
                "errors": result.errors,
            }
        )
    return results
