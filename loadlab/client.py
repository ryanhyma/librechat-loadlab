"""Async LibreChat client: auth, chat over SSE, reads, search, uploads, RAG queries."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from . import errs
from .config import UA

NO_PARENT = "00000000-0000-0000-0000-000000000000"


@dataclass
class Turn:
    ok: bool = False
    status: int = 0
    error_class: str = "ok"
    limiter: str | None = None
    detail: str = ""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    events: int = 0
    text: str = ""
    reasoning_chars: int = 0
    conversation_id: str | None = None
    response_message_id: str | None = None
    stream_id: str | None = None
    started_at: float = field(default_factory=time.time)


@dataclass
class ProbeResult:
    ok: bool
    kind: str
    latency_ms: float | None = None
    error_class: str = "ok"
    detail: str = ""
    body: str = ""


class LibreChatClient:
    """One user session. Tokens are bearer JWTs returned by /api/auth/login."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        endpoint: str = "local27b",
        model: str = "qwen3.6-27b",
        endpoint_type: str = "custom",
        agent_id: str | None = None,
        timeout: float = 180.0,
        turn_timeout: float = 180.0,
        idle_timeout: float = 20.0,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.endpoint = endpoint
        self.model = model
        self.endpoint_type = endpoint_type
        self.agent_id = agent_id
        self.turn_timeout = turn_timeout
        self.idle_timeout = idle_timeout
        self.http = httpx.AsyncClient(
            base_url=self.base,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
        )

    # ---------------------------------------------------------------- auth
    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def login(self, email: str, password: str) -> tuple[bool, str]:
        r = await self.http.post("/api/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            cls, lim, detail = errs.classify_http(r.status_code, _json(r))
            return False, f"{cls}:{detail or r.status_code}"
        self.token = r.json().get("token") or ""
        return bool(self.token), ""

    async def close(self) -> None:
        await self.http.aclose()

    # ---------------------------------------------------------------- chat
    async def chat(
        self,
        text: str,
        conversation_id: str | None = None,
        parent_message_id: str = NO_PARENT,
        abort_after_ms: int | None = None,
    ) -> Turn:
        turn = Turn(conversation_id=conversation_id)
        message_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "text": text,
            "sender": "User",
            "isCreatedByUser": True,
            "parentMessageId": parent_message_id,
            "conversationId": conversation_id or "new",
            "messageId": message_id,
            "responseMessageId": f"{message_id}_",
            "endpoint": self.endpoint,
            "endpointType": self.endpoint_type,
            "model": self.model,
            "isTemporary": False,
            "isRegenerate": False,
            "error": False,
        }
        if self.agent_id:
            payload["agent_id"] = self.agent_id
            payload["endpointType"] = "agents"
        t0 = time.perf_counter()
        path = "/api/agents/chat" if self.agent_id else f"/api/agents/chat/{self.endpoint}"
        try:
            r = await self.http.post(path, json=payload, headers=self.auth)
        except httpx.HTTPError as exc:
            turn.total_ms = (time.perf_counter() - t0) * 1000
            turn.error_class, turn.detail = "stream_disconnect", f"post failed: {exc}"[:300]
            return turn

        if r.status_code != 200 or "event-stream" in r.headers.get("content-type", ""):
            # rate-limit / validation errors are returned as an SSE error or JSON
            body = _json(r)
            if isinstance(body, dict) and body.get("streamId"):
                turn.stream_id = body["streamId"]
                turn.conversation_id = body.get("conversationId")
            else:
                turn.status = r.status_code
                turn.total_ms = (time.perf_counter() - t0) * 1000
                turn.error_class, turn.limiter, turn.detail = errs.classify_http(r.status_code, body)
                if turn.error_class == "ok":  # streamed error frame on the POST itself
                    turn.error_class, turn.detail = "sse_error", str(body)[:300]
                return turn

        start = _json(r) or {}
        if isinstance(start, dict) and start.get("streamId"):
            turn.stream_id = start["streamId"]
            turn.conversation_id = start.get("conversationId") or conversation_id
            aborter = None
            if abort_after_ms and turn.conversation_id:
                aborter = asyncio.create_task(self._abort_later(turn.conversation_id, abort_after_ms))
            try:
                await self._consume_stream(turn, t0)
            except httpx.HTTPError as exc:
                turn.total_ms = (time.perf_counter() - t0) * 1000
                turn.error_class, turn.detail = "stream_disconnect", str(exc)[:300]
            finally:
                if aborter:
                    aborter.cancel()
        elif turn.conversation_id is None:
            turn.conversation_id = message_id
        turn.total_ms = turn.total_ms or (time.perf_counter() - t0) * 1000
        turn.ok = turn.error_class == "ok"
        return turn

    async def _abort_later(self, conversation_id: str, delay_ms: int) -> None:
        try:
            await asyncio.sleep(delay_ms / 1000)
            await self.abort(conversation_id)
        except (asyncio.CancelledError, httpx.HTTPError):
            pass

    async def _consume_stream(self, turn: Turn, t0: float) -> None:
        assert turn.stream_id
        url = f"/api/agents/chat/stream/{turn.stream_id}?resume=true"
        deadline = t0 + self.turn_timeout
        async with self.http.stream(
            "GET", url, headers={**self.auth, "Accept": "text/event-stream"}, timeout=self.turn_timeout
        ) as resp:
            turn.status = resp.status_code
            if resp.status_code != 200:
                body = await resp.aread()
                turn.error_class, turn.limiter, turn.detail = errs.classify_http(
                    resp.status_code, _json_bytes(body)
                )
                return
            event_name, data_lines = "", []
            last_event = time.perf_counter()
            async for line in resp.aiter_lines():
                if line.startswith("event:") or line.startswith("data:"):
                    last_event = time.perf_counter()
                if time.perf_counter() - last_event > self.idle_timeout:
                    turn.error_class, turn.detail = "stream_stall", f"no SSE data for {self.idle_timeout:.0f}s"
                    return
                if time.perf_counter() > deadline:
                    turn.error_class, turn.detail = "stream_disconnect", "turn timeout"
                    return
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and (event_name or data_lines):
                    payload = _parse("".join(data_lines))
                    terminal = self._handle_event(turn, event_name, payload, t0)
                    event_name, data_lines = "", []
                    if terminal:
                        return
            if turn.error_class == "ok" and not turn.text and not turn.reasoning_chars:
                turn.error_class, turn.detail = "stream_disconnect", "stream closed without content"

    def _handle_event(self, turn: Turn, event_name: str, payload: Any, t0: float) -> bool:
        turn.events += 1
        now = (time.perf_counter() - t0) * 1000
        if event_name == "error":
            detail = str(payload.get("message") if isinstance(payload, dict) else payload)
            turn.error_class = errs._upstream_class(detail) or "sse_error"
            turn.detail = detail[:300]
            return True
        if not isinstance(payload, dict):
            return False
        if payload.get("final"):
            turn.error_class, turn.limiter, turn.detail = errs.classify_final(payload)
            response = payload.get("responseMessage") or {}
            if isinstance(response, dict):
                turn.response_message_id = response.get("messageId")
                if response.get("text") and not turn.text:
                    turn.text = str(response["text"])
            turn.conversation_id = (
                (payload.get("conversation") or {}).get("conversationId") or turn.conversation_id
            )
            return True
        if payload.get("created"):
            turn.conversation_id = (payload.get("message") or {}).get("conversationId") or turn.conversation_id
            return False
        inner = payload.get("event")
        if not inner:
            return False
        if inner in ("on_message_delta", "on_reasoning_delta"):
            if turn.ttft_ms is None:
                turn.ttft_ms = now
            chunk = payload.get("data") or {}
            text = _delta_text(chunk)
            if inner == "on_message_delta":
                turn.text += text
            else:
                turn.reasoning_chars += len(text)
        elif inner == "on_run_step_completed":
            data = payload.get("data") or {}
            output = data.get("result") or data.get("output")
            if errs.is_mcp_tool_error(output):
                turn.error_class = "mcp_tool_error"
                turn.detail = str(output)[:300]
        return False

    # ---------------------------------------------------------------- abort
    async def abort(self, conversation_id: str) -> tuple[int, Any]:
        r = await self.http.post(
            "/api/agents/chat/abort", json={"conversationId": conversation_id}, headers=self.auth
        )
        return r.status_code, _json(r)

    # ---------------------------------------------------------------- reads
    async def conversations(self, page: int = 0) -> tuple[int, Any]:
        r = await self.http.get(f"/api/convos?pageNumber={page}", headers=self.auth)
        return r.status_code, _json(r)

    async def messages(self, conversation_id: str) -> tuple[int, Any]:
        r = await self.http.get(f"/api/messages/{conversation_id}", headers=self.auth)
        return r.status_code, _json(r)

    async def search_messages(self, query: str) -> tuple[int, Any]:
        r = await self.http.get("/api/messages", params={"search": query}, headers=self.auth)
        return r.status_code, _json(r)

    async def search_conversations(self, query: str) -> tuple[int, Any]:
        r = await self.http.get("/api/convos", params={"search": query}, headers=self.auth)
        return r.status_code, _json(r)

    # ---------------------------------------------------------------- files
    async def upload(self, filename: str, content: bytes) -> tuple[int, Any]:
        files = {"file": (filename, content, "text/plain")}
        data = {
            "endpoint": self.endpoint,
            "model": self.model,
            "file_id": str(uuid.uuid4()),
            "message_file": "true",
        }
        r = await self.http.post("/api/files", files=files, data=data, headers=self.auth)
        return r.status_code, _json(r)

    async def upload_bad(self, filename: str, content: bytes) -> tuple[int, Any]:
        """Deliberately invalid upload (non-UUID file_id) — exercises the 500 path."""
        r = await self.http.post(
            "/api/files",
            files={"file": (filename, content, "text/plain")},
            data={"endpoint": self.endpoint, "file_id": "not-a-uuid"},
            headers=self.auth,
        )
        return r.status_code, _json(r)

    async def provider_probe(self, provider_url: str, model: str, api_key: str = "") -> ProbeResult:
        """Call the model tier directly (Azure/LiteLLM/Strata) — 429 lives here, not in SSE."""
        t0 = time.perf_counter()
        try:
            r = await self.http.post(
                f"{provider_url.rstrip('/')}/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "stream": False, "max_tokens": 8},
                headers={"Authorization": f"Bearer {api_key or 'loadlab'}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            return ProbeResult(False, "provider", error_class="probe_error", detail=str(exc)[:200])
        latency = (time.perf_counter() - t0) * 1000
        body = _json(r)
        if r.status_code >= 400:
            cls, _, detail = errs.classify_http(r.status_code, body)
            if r.status_code == 429:
                cls = "provider_429"
            elif r.status_code == 503:
                cls = "provider_unavailable"
            retry = r.headers.get("retry-after")
            if retry:
                detail = f"retry-after={retry}s {detail}"
            return ProbeResult(False, "provider", latency, cls, detail)
        return ProbeResult(True, "provider", latency, "ok", "")

    async def rag_embed(self, file_id: str, filename: str, content: bytes) -> ProbeResult:
        """Talk to rag_api directly: ingestion is where 429/401/500 actually originate."""
        rag = _rag_url(self.base)
        t0 = time.perf_counter()
        try:
            r = await self.http.post(
                f"{rag}/embed",
                data={"file_id": file_id},
                files={"file": (filename, content, "text/plain")},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            return ProbeResult(False, "rag_embed", error_class="probe_error", detail=str(exc)[:200])
        latency = (time.perf_counter() - t0) * 1000
        body = _json(r)
        if r.status_code != 200:
            cls, _, detail = errs.classify_http(r.status_code, body)
            return ProbeResult(False, "rag_embed", latency, cls, detail)
        ok = bool(isinstance(body, dict) and body.get("status"))
        detail = "" if ok else str((body or {}).get("message") or body)[:300]
        return ProbeResult(ok, "rag_embed", latency, "ok" if ok else "silent_rag", detail, body=str(body))

    async def rag_query(self, file_id: str, query: str, *, token: str | None = None) -> ProbeResult:
        """Direct rag_api query — proves the vector copy is queryable."""
        rag = _rag_url(self.base)
        t0 = time.perf_counter()
        try:
            r = await self.http.post(
                f"{rag}/query",
                json={"file_id": file_id, "query": query, "k": 4},
                headers={"Authorization": f"Bearer {token or self.token}"},
            )
        except httpx.HTTPError as exc:
            return ProbeResult(False, "rag_query", error_class="probe_error", detail=str(exc)[:200])
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            cls, _, detail = errs.classify_http(r.status_code, _json(r))
            return ProbeResult(False, "rag_query", latency, cls, detail)
        return ProbeResult(True, "rag_query", latency, "ok", "", body=r.text[:20000])


# ---------------------------------------------------------------- helpers
def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"message": resp.text[:300]}


def _json_bytes(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return {"message": body.decode("utf-8", "replace")[:300]}


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw[:300]


def _delta_text(chunk: dict[str, Any]) -> str:
    delta = chunk.get("delta") or {}
    content = delta.get("content") or []
    out = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict):
            out.append(str(part.get("text") or part.get("think") or ""))
    return "".join(out)


def _rag_url(base: str) -> str:
    """rag_api lives next to LibreChat in dev; override with LOADLAB_RAG_URL."""
    import os

    return os.environ.get("LOADLAB_RAG_URL", "http://172.18.0.5:8000").rstrip("/")


async def stream_events(client: LibreChatClient, url: str) -> AsyncIterator[tuple[str, Any]]:
    """Expose raw SSE events (used by tests and the live tail)."""
    async with client.http.stream(
        "GET", url, headers={**client.auth, "Accept": "text/event-stream"}
    ) as resp:
        name, data = "", []
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "" and (name or data):
                yield name, _parse("".join(data))
                name, data = "", []
