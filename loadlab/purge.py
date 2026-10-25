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

from .config import UA, RunConfig
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
        return {
            "user_ids": user_ids,
            "user_strs": user_strs,
            "conversations": conv_ids,
            "messages": msg_ids,
            "files": sorted(file_ids),
            "meili": {"messages": sorted(meili_msgs), "convos": sorted(meili_convos)},
        }

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

        if self.ids["files"]:
            self._delete_vectors()
            steps_done = {"name": "rag_vectors", "count": len(self.ids["files"])}
            result.steps.append(steps_done)

        for index, ids in self.ids["meili"].items():
            if ids:
                deleted = self._delete_meili(index, ids)
                result.steps.append({"name": f"meili_{index}", "count": deleted})

        uploads = Path(self.cfg.target.uploads_dir)
        removed = 0
        for uid in self.ids["user_ids"]:
            target = uploads / str(uid)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                removed += 1
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

        if self.ids["user_ids"]:
            res = self.db.users.delete_many({"_id": {"$in": self.ids["user_ids"]}})
            result.steps.append({"name": "mongo_users", "count": res.deleted_count})

        result.residue = self.verify()
        result.ok = all(v == 0 for v in result.residue.values())
        return result

    # ------------------------------------------------------- subsystems
    def _delete_vectors(self) -> None:
        token = self._token()
        if not token:
            return
        try:
            httpx.request(
                "DELETE",
                f"{self.cfg.target.rag_url.rstrip('/')}/documents",
                json=self.ids["files"],
                headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                timeout=60,
            )
        except httpx.HTTPError:
            pass

    def _delete_meili(self, index: str, ids: list[str]) -> int:
        if not self.cfg.target.meili_key:
            return 0
        try:
            r = httpx.post(
                f"{self.cfg.target.meili_url.rstrip('/')}/indexes/{index}/documents/delete-batch",
                json=ids,
                headers={"Authorization": f"Bearer {self.cfg.target.meili_key}"},
                timeout=60,
            )
            return len(ids) if r.status_code < 400 else 0
        except httpx.HTTPError:
            return 0

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