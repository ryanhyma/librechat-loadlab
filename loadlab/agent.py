"""LibreChat Agents with the native `file_search` tool (RAG through the app).

Flow verified against the running stack:
  POST /api/agents            -> agent_id
  POST /api/files (uuid file_id, endpoint=agents, agent_id, tool_resource=file_search)
                              -> server assigns the id and attaches it to
                                 agent.tool_resources.file_search.file_ids
  POST /api/agents/chat       -> SSE; file_search hits rag_api, the answer carries the content

No agent PATCH is needed; the upload request attaches the file.
"""

from __future__ import annotations

import uuid
from typing import Any

from .client import LibreChatClient, Turn


async def create_agent(
    client: LibreChatClient, name: str, provider: str, model: str
) -> tuple[str | None, int, str]:
    r = await client.http.post(
        "/api/agents",
        headers=client.auth,
        json={
            "name": name,
            "provider": provider,
            "model": model,
            "model_parameters": {},
            "tools": ["file_search"],
            "instructions": "Use file_search to answer questions about the uploaded files.",
        },
    )
    if r.status_code >= 300:
        return None, r.status_code, r.text[:200]
    return r.json().get("id"), r.status_code, ""


async def upload_agent_file(
    client: LibreChatClient, agent_id: str, filename: str, content: bytes
) -> tuple[str | None, bool, int, str]:
    """Returns (file_id assigned by the server, embedded, status, detail)."""
    r = await client.http.post(
        "/api/files",
        headers=client.auth,
        files={"file": (filename, content, "text/plain")},
        data={
            "endpoint": "agents",
            "agent_id": agent_id,
            "tool_resource": "file_search",
            "file_id": str(uuid.uuid4()),
        },
    )
    if r.status_code >= 300:
        return None, False, r.status_code, r.text[:200]
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    file_id = (body or {}).get("file_id") or (body or {}).get("id")
    return file_id, bool((body or {}).get("embedded")), r.status_code, ""


async def file_search_turn(
    client: LibreChatClient, agent_id: str, question: str, conversation_id: str | None = None
) -> Turn:
    client.endpoint = "agents"
    client.agent_id = agent_id
    return await client.chat(question, conversation_id)


def answered_with(turn: Turn, token: str) -> bool:
    return token in (turn.text or "")


async def run_agent_probe(
    client: LibreChatClient, provider: str, model: str, token: str, filename: str, content: bytes
) -> dict[str, Any]:
    """One-shot: create agent, attach a fixture, ask for its codename."""
    agent_id, status, detail = await create_agent(client, f"loadlab {uuid.uuid4().hex[:6]}", provider, model)
    if not agent_id:
        return {"ok": False, "stage": "create", "status": status, "detail": detail}
    file_id, embedded, status, detail = await upload_agent_file(client, agent_id, filename, content)
    if not file_id or not embedded:
        return {"ok": False, "stage": "upload", "status": status, "detail": detail, "agent_id": agent_id}
    turn = await file_search_turn(client, agent_id, "What is the deployment codename? Reply with only the codename.")
    return {
        "ok": answered_with(turn, token),
        "stage": "answer",
        "agent_id": agent_id,
        "file_id": file_id,
        "turn": turn,
    }
