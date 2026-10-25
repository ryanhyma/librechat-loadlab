"""Run ledger: append-only record of everything a run owns (PLAN.md 5.1)."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .config import RUNS_DIR, RunConfig


class Ledger:
    def __init__(self, cfg: RunConfig) -> None:
        self.cfg = cfg
        self.short_id = secrets.token_hex(3)
        self.run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{cfg.name}-{self.short_id}"
        self.dir = RUNS_DIR / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "ledger.json"
        self.data: dict[str, Any] = {
            "run_id": self.run_id,
            "short_id": self.short_id,
            "created_at": time.time(),
            "config": cfg.model_dump(),
            "users": [],           # [{index, id, email, password}]
            "conversations": [],   # discovered during the run
            "files": [],           # [{file_id, filename, token}]
            "meili": {"messages": [], "convos": []},
            "status": "running",
            "finished_at": None,
            "notes": [],
        }
        self.flush()

    # ---------------------------------------------------------------- io
    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        os.replace(tmp, self.path)

    def _append(self, key: str, value: Any) -> None:
        bucket = self.data.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)

    # ------------------------------------------------------------ record
    def add_user(self, index: int, user_id: str, email: str, password: str) -> None:
        self.data["users"].append(
            {"index": index, "id": user_id, "email": email, "password": password}
        )
        self.flush()

    def add_conversation(self, conversation_id: str) -> None:
        if conversation_id:
            self._append("conversations", conversation_id)

    def add_file(self, file_id: str, filename: str, token: str, user: int | None = None) -> None:
        self.data["files"].append(
            {"file_id": file_id, "filename": filename, "token": token, "user_index": user}
        )
        self.flush()

    def add_meili(self, index: str, doc_id: str) -> None:
        self.data["meili"].setdefault(index, [])
        if doc_id not in self.data["meili"][index]:
            self.data["meili"][index].append(doc_id)

    def note(self, text: str) -> None:
        self.data["notes"].append({"t": time.time(), "text": text})
        self.flush()

    def finish(self, status: str = "finished") -> None:
        self.data["status"] = status
        self.data["finished_at"] = time.time()
        self.flush()

    # ------------------------------------------------------------- load
    @staticmethod
    def load(run_id: str) -> dict[str, Any]:
        path = RUNS_DIR / run_id / "ledger.json"
        return json.loads(path.read_text())

    @staticmethod
    def runs() -> list[str]:
        if not RUNS_DIR.exists():
            return []
        return sorted(p.name for p in RUNS_DIR.iterdir() if (p / "ledger.json").exists())
