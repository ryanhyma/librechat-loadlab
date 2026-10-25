"""Virtual users: provisioning, login, archetype actions, silent-failure probes."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import secrets
import time
from typing import Any

import bcrypt
from pymongo import MongoClient

from . import errs
from .client import LibreChatClient, NO_PARENT, Turn
from .config import RunConfig
from .ledger import Ledger
from .store import Sample, Store, sleep_jitter

PASSWORD = "loadlab-pass-2026"
NO_PARENT_ID = NO_PARENT


# ---------------------------------------------------------------- provisioning
def provision_users(cfg: RunConfig, ledger: Ledger, count: int) -> list[dict[str, Any]]:
    """Clone a real local user doc so test accounts are app-identical."""
    client = MongoClient(cfg.target.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client.get_default_database()
    template = db.users.find_one({"provider": "local"}) or {}
    now = dt.datetime.now(dt.timezone.utc)
    pwd = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(10)).decode()
    users = []
    for i in range(count):
        email = f"lt+{ledger.short_id}-{i}@loadlab.invalid"
        doc = {
            k: v
            for k, v in template.items()
            if k
            not in (
                "_id",
                "refreshToken",
                "backupCodes",
                "password",
                "twoFactorEnabled",
                "twoFactorEnrolledAt",
                "twoFactorAcknowledgementNonceHash",
                "twoFactorFinalizationNonceHash",
                "createdAt",
                "updatedAt",
                "__v",
                "tenantId",
                "orgId",
            )
        }
        doc.update(
            name=f"loadlab {ledger.run_id} {i}",
            username="",
            email=email,
            emailVerified=True,
            provider="local",
            role="USER",
            password=pwd,
            createdAt=now,
            updatedAt=now,
            _loadlab_run=ledger.run_id,
        )
        db.users.delete_many({"email": email})
        res = db.users.insert_one(doc)
        ledger.add_user(i, str(res.inserted_id), email, PASSWORD)
        users.append({"index": i, "id": str(res.inserted_id), "email": email})
    client.close()
    return users


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, cfg: RunConfig, store: Store, ledger: Ledger) -> None:
        self.cfg = cfg
        self.store = store
        self.ledger = ledger
        self.stop = asyncio.Event()
        self.tokens: dict[int, str] = {}
        self.credentials: dict[int, tuple[str, str]] = {}
        self.sem = asyncio.Semaphore(8)
        self._weights = [(name, item.weight) for name, item in cfg.mix.items()]

    # ------------------------------------------------------------- setup
    async def _login_all(self, users: list[dict[str, Any]]) -> dict[int, LibreChatClient]:
        clients: dict[int, LibreChatClient] = {}

        async def one(user: dict[str, Any]) -> None:
            client = self._client()
            async with self.sem:
                ok, detail = await client.login(user["email"], PASSWORD)
            if not ok:
                self.store.add(
                    Sample(time.time(), "auth", False, 0, error_class="auth", detail=detail, user=user["index"])
                )
                await client.close()
                return
            clients[user["index"]] = client
            self.credentials[user["index"]] = (user["email"], PASSWORD)

        await asyncio.gather(*(one(u) for u in users))
        return clients

    def _client(self) -> LibreChatClient:
        t = self.cfg.target
        return LibreChatClient(
            t.base_url,
            endpoint=t.endpoint,
            model=t.model,
            endpoint_type=t.endpoint_type,
            turn_timeout=240.0 if "local" in t.endpoint else 60.0,
            idle_timeout=45.0 if "local" in t.endpoint else 20.0,
        )

    # --------------------------------------------------------------- run
    async def run(self) -> None:
        users = provision_users(self.cfg, self.ledger, self.cfg.users)
        clients = await self._login_all(users)
        if not clients:
            self.store.stop_reason = "no test user could log in"
            self.store.finished = True
            self.ledger.finish("failed")
            return
        self.store.users_active = len(clients)
        stagger = self.cfg.ramp_s / max(1, len(clients))
        tasks = [
            asyncio.create_task(self._vu(index, client, stagger * i))
            for i, (index, client) in enumerate(sorted(clients.items()))
        ]
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.duration_s)
            self.store.stop_reason = "stopped"
        except asyncio.TimeoutError:
            self.store.stop_reason = "duration"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for client in clients.values():
                await client.close()
            self.store.users_active = 0
            self.store.finished = True
            self.ledger.finish(self.store.stop_reason)

    async def shutdown(self) -> None:
        self.stop.set()

    # --------------------------------------------------------------- VU
    async def _vu(self, index: int, client: LibreChatClient, delay: float) -> None:
        if delay:
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
        rng = random.Random(index * 7919)
        state = {"conversation_id": None, "parent_id": NO_PARENT_ID, "turns": 0}
        while not self.stop.is_set():
            archetype = self._pick(rng)
            item = self.cfg.mix[archetype]
            try:
                await self._act(archetype, item, index, client, state, rng)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never kill a VU on a harness bug
                self.store.add(
                    Sample(time.time(), archetype, False, 0, "unknown", detail=str(exc)[:200], user=index)
                )
            wait = sleep_jitter(item.think_ms or self.cfg.think_ms)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def _pick(self, rng: random.Random) -> str:
        names = [n for n, _ in self._weights]
        weights = [w for _, w in self._weights]
        return rng.choices(names, weights=weights, k=1)[0]

    # ----------------------------------------------------------- actions
    async def _act(
        self,
        action: str,
        item: Any,
        index: int,
        client: LibreChatClient,
        state: dict[str, Any],
        rng: random.Random,
    ) -> None:
        if action == "chatty":
            await self._chat(index, client, state, item, rng)
        elif action == "reader":
            await self._read(index, client, state)
        elif action == "search":
            await self._search(index, client, rng)
        elif action == "rag":
            await self._rag(index, client, rng)
        elif action == "impatient":
            await self._impatient(index, client, state, item, rng)
        elif action == "auth":
            await self._auth(index, client, rng)
        else:
            await self._chat(index, client, state, item, rng)

    async def _chat(
        self, index: int, client: LibreChatClient, state: dict[str, Any], item: Any, rng: random.Random
    ) -> None:
        marker = f"LL{secrets.token_hex(4)}"
        if state["turns"] >= max(1, item.turns):
            state.update(conversation_id=None, parent_id=NO_PARENT_ID, turns=0)
        prompt = f"{item.prompt} Ref: {marker}"
        self.store.enter("chat")
        try:
            turn = await client.chat(prompt, state["conversation_id"], state["parent_id"])
        finally:
            self.store.leave("chat")
        self._record("chat", index, turn)
        if turn.ok:
            state["turns"] += 1
            state["conversation_id"] = turn.conversation_id or state["conversation_id"]
            state["parent_id"] = turn.response_message_id or state["parent_id"]
            if state["conversation_id"]:
                self.ledger.add_conversation(state["conversation_id"])
            if self.cfg.probe_meili and rng.random() < 0.35 and state["conversation_id"]:
                await self._probe_meili(index, client, marker)

    async def _read(self, index: int, client: LibreChatClient, state: dict[str, Any]) -> None:
        convo = state.get("conversation_id")
        self.store.enter("read")
        try:
            t0 = time.perf_counter()
            status, body = await client.conversations()
            total = (time.perf_counter() - t0) * 1000
            if status >= 400:
                cls, lim, detail = errs.classify_http(status, body)
                self.store.add(Sample(time.time(), "read", False, status, cls, lim, detail, total_ms=total, user=index))
                return
            self.store.add(Sample(time.time(), "read", True, status, total_ms=total, user=index))
            if convo:
                t0 = time.perf_counter()
                status, body = await client.messages(convo)
                total = (time.perf_counter() - t0) * 1000
                cls, lim, detail = errs.classify_http(status, body)
                self.store.add(
                    Sample(time.time(), "read", status < 400, status, cls, lim, detail, total_ms=total, user=index)
                )
        finally:
            self.store.leave("read")

    async def _search(self, index: int, client: LibreChatClient, rng: random.Random) -> None:
        term = rng.choice(["load", "test", "LL", "summary"])
        self.store.enter("search")
        try:
            t0 = time.perf_counter()
            status, body = await client.search_messages(term)
            total = (time.perf_counter() - t0) * 1000
            cls, lim, detail = errs.classify_http(status, body)
            self.store.add(Sample(time.time(), "search", status < 400, status, cls, lim, detail, total_ms=total, user=index))
        finally:
            self.store.leave("search")

    async def _rag(self, index: int, client: LibreChatClient, rng: random.Random) -> None:
        token = f"RAG{secrets.token_hex(5)}"
        body = f"loadlab rag fixture {token}\n" + ("lorem ipsum " * max(1, self.cfg.mix["rag"].file_kb // 2))
        self.store.enter("upload")
        try:
            t0 = time.perf_counter()
            status, payload = await client.upload(f"loadlab-{token}.txt", body.encode())
            latency = (time.perf_counter() - t0) * 1000
        finally:
            self.store.leave("upload")
        cls, lim, detail = errs.classify_http(status, payload)
        self.store.add(
            Sample(time.time(), "upload", status < 400, status, cls, lim, detail, total_ms=latency, user=index)
        )
        if not self.cfg.probe_rag:
            return
        file_id = str(__import__("uuid").uuid4())
        embed = await client.rag_embed(file_id, f"loadlab-{token}.txt", body.encode())
        self.store.add_probe("rag_embed", embed.ok, embed.latency_ms, embed.error_class, embed.detail)
        if not embed.ok:
            return
        self.ledger.add_file(file_id, f"loadlab-{token}.txt", token, user=index)
        query = await client.rag_query(file_id, token)
        if not query.ok:
            self.store.add_probe("rag_query", False, query.latency_ms, query.error_class, query.detail)
        elif token not in query.body:
            self.store.add_probe("rag_query", False, query.latency_ms, "silent_rag", "query returned no matching content")
        else:
            self.store.add_probe("rag_query", True, query.latency_ms, "ok", "")

    async def _impatient(
        self, index: int, client: LibreChatClient, state: dict[str, Any], item: Any, rng: random.Random
    ) -> None:
        self.store.enter("abort")
        try:
            turn = await client.chat(
                f"Short reply please. Ref: AB{secrets.token_hex(3)}",
                None,
                NO_PARENT_ID,
                abort_after_ms=item.abort_after_ms,
            )
            if turn.conversation_id:
                await client.abort(turn.conversation_id)
            self.store.add(
                Sample(
                    time.time(),
                    "abort",
                    True,
                    turn.status,
                    total_ms=turn.total_ms,
                    ttft_ms=turn.ttft_ms,
                    events=turn.events,
                    user=index,
                )
            )
        finally:
            self.store.leave("abort")

    async def _auth(self, index: int, client: LibreChatClient, rng: random.Random) -> None:
        t0 = time.perf_counter()
        email, password = self.credentials.get(index, ("", ""))
        ok, detail = await client.login(email, password)
        self.store.add(
            Sample(
                time.time(),
                "auth",
                ok,
                200 if ok else 401,
                "ok" if ok else "auth",
                detail=detail,
                total_ms=(time.perf_counter() - t0) * 1000,
                user=index,
            )
        )

    def _record(self, action: str, index: int, turn: Turn) -> None:
        self.store.add(
            Sample(
                t=time.time(),
                action=action,
                ok=turn.ok,
                status=turn.status,
                error_class=turn.error_class,
                limiter=turn.limiter,
                detail=turn.detail,
                ttft_ms=turn.ttft_ms,
                total_ms=turn.total_ms,
                events=turn.events,
                user=index,
                conversation_id=turn.conversation_id,
                chars=len(turn.text),
            )
        )

    # ------------------------------------------------------------ probes
    async def _wait_embedded(self, file_id: str, timeout: float) -> bool:
        def check() -> bool:
            client = MongoClient(self.cfg.target.mongo_uri, serverSelectionTimeoutMS=5000)
            try:
                doc = client.get_default_database().files.find_one({"file_id": file_id})
                return bool(doc and doc.get("embedded"))
            finally:
                client.close()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if await asyncio.to_thread(check):
                return True
            await asyncio.sleep(1.0)
        return False

    async def _probe_meili(self, index: int, client: LibreChatClient, marker: str) -> None:
        """Meili failures are silent: prove the marker is findable, and how fast."""
        t0 = time.time()
        while time.time() - t0 < 15:
            status, body = await client.search_messages(marker)
            if status < 400 and marker in str(body):
                self.store.add_probe("meili_lag", True, (time.time() - t0) * 1000, "ok", marker)
                return
            await asyncio.sleep(1.0)
        self.store.add_probe("meili_lag", False, None, "silent_meili", f"{marker} not indexed in 15s")
