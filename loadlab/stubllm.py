"""Deterministic OpenAI-compatible SSE stub: measure LibreChat without the GPU."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TTFT_MS = int(os.environ.get("LOADLAB_STUB_TTFT_MS", "120"))
TPS = float(os.environ.get("LOADLAB_STUB_TPS", "60"))
MAX_TOKENS = int(os.environ.get("LOADLAB_STUB_TOKENS", "48"))
MODE = os.environ.get("LOADLAB_STUB_MODE", "normal")  # normal|error500|error429|disconnect|slow

app = FastAPI(title="loadlab stub LLM")


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "loadlab-stub", "object": "model"}]}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "mode": MODE, "ttft_ms": TTFT_MS, "tps": TPS, "max_tokens": MAX_TOKENS}


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    if MODE == "error500":
        return JSONResponse({"error": {"message": "stub upstream 500"}}, status_code=500)
    if MODE == "error429":
        return JSONResponse({"error": {"message": "stub upstream rate limited"}}, status_code=429)
    stream = body.get("stream", True)

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
        text = "loadlab stub reply " + "token " * MAX_TOKENS
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
