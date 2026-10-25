"""Deterministic OpenAI-compatible SSE stub: measure LibreChat without the GPU."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TTFT_MS = int(os.environ.get("LOADLAB_STUB_TTFT_MS", "120"))
TPS = float(os.environ.get("LOADLAB_STUB_TPS", "60"))
MAX_TOKENS = int(os.environ.get("LOADLAB_STUB_TOKENS", "48"))
MODE = os.environ.get("LOADLAB_STUB_MODE", "normal")  # normal|error500|error429|disconnect|slow|azure
RPM = int(os.environ.get("LOADLAB_STUB_RPM", "0"))      # azure mode: requests per minute (0=off)
TPM = int(os.environ.get("LOADLAB_STUB_TPM", "0"))      # azure mode: tokens per minute (0=off)
RETRY_AFTER = int(os.environ.get("LOADLAB_STUB_RETRY_AFTER", "3"))
_BUCKET: list[tuple[float, int]] = []

app = FastAPI(title="loadlab stub LLM")


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "loadlab-stub", "object": "model"}]}


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "mode": MODE,
        "ttft_ms": TTFT_MS,
        "tps": TPS,
        "max_tokens": MAX_TOKENS,
        "rpm": RPM,
        "tpm": TPM,
    }


def _azure_headers(remaining_req: int, remaining_tok: int) -> dict[str, str]:
    return {
        "x-ratelimit-remaining-requests": str(max(0, remaining_req)),
        "x-ratelimit-remaining-tokens": str(max(0, remaining_tok)),
    }


def _azure_guard(tokens: int) -> JSONResponse | None:
    """Azure-style budget: 429 + Retry-After instead of queueing."""
    now = time.time()
    while _BUCKET and now - _BUCKET[0][0] > 60:
        _BUCKET.pop(0)
    used_req = len(_BUCKET)
    used_tok = sum(t for _, t in _BUCKET)
    if RPM and used_req >= RPM:
        return JSONResponse(
            {"error": {"message": f"Rate limit reached for requests per minute ({RPM}).", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            status_code=429,
            headers={"Retry-After": str(RETRY_AFTER), **_azure_headers(RPM - used_req, TPM - used_tok)},
        )
    if TPM and used_tok + tokens > TPM:
        return JSONResponse(
            {"error": {"message": f"Rate limit reached for tokens per minute ({TPM}).", "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            status_code=429,
            headers={"Retry-After": str(RETRY_AFTER), **_azure_headers(RPM - used_req, TPM - used_tok)},
        )
    _BUCKET.append((now, tokens))
    return None


TOKEN_RE = re.compile(r"(?:RAGTOKEN|AGENTRAG|RAGTEST)[A-Za-z0-9]{4,}")


def _tool_names(body: dict) -> list[str]:
    names = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.append(fn["name"])
    return names


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else json.dumps(content)[:500]
    return ""


def _tool_result_text(body: dict) -> str:
    parts = []
    for msg in body.get("messages") or []:
        if msg.get("role") == "tool":
            content = msg.get("content")
            parts.append(content if isinstance(content, str) else json.dumps(content))
    return "\n".join(parts)[:20000]


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    try:
        import pathlib as _p

        _p.Path("/tmp/loadlab-stub-last.json").write_text(json.dumps(body)[:200000])
    except Exception:
        pass
    if MODE == "error500":
        return JSONResponse({"error": {"message": "stub upstream 500"}}, status_code=500)
    if MODE == "error429":
        return JSONResponse({"error": {"message": "stub upstream rate limited"}}, status_code=429)
    if MODE == "azure":
        prompt = "".join(str(m.get("content") or "") for m in body.get("messages") or [])
        budget = max(MAX_TOKENS, len(prompt) // 4)
        limited = _azure_guard(budget)
        if limited is not None:
            return limited
    stream = body.get("stream", True)
    tool_text = _tool_result_text(body)
    match = TOKEN_RE.search(tool_text)
    wants_file_search = "file_search" in _tool_names(body)
    needs_tool_call = wants_file_search and match is None and not tool_text.strip()

    async def gen():
        base = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.get("model", "loadlab-stub"),
        }
        await asyncio.sleep(TTFT_MS / 1000)
        if MODE == "disconnect":
            return
        delay = 1.0 / max(TPS, 1)
        if needs_tool_call:
            yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
            call = {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_{uuid.uuid4().hex[:10]}",
                                    "type": "function",
                                    "function": {
                                        "name": "file_search",
                                        "arguments": json.dumps({"query": _last_user_text(body)[:200]}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(call)}\n\n"
            stop = {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 16, "completion_tokens": 8, "total_tokens": 24},
            }
            yield f"data: {json.dumps(stop)}\n\n"
            yield "data: [DONE]\n\n"
            return
        text = match.group(0) if match else "loadlab stub reply " + "token " * MAX_TOKENS
        words = text.split()
        for i, word in enumerate(words[:MAX_TOKENS]):
            chunk = {
                **base,
                "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(delay * (4 if MODE == "slow" else 1))
        done = {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 16,
                "completion_tokens": len(words[:MAX_TOKENS]),
                "total_tokens": 16 + len(words[:MAX_TOKENS]),
            },
        }
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"

    if not stream:
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "loadlab-stub"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "loadlab stub reply"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 16, "completion_tokens": 4, "total_tokens": 20},
            }
        )
    return StreamingResponse(gen(), media_type="text/event-stream")
