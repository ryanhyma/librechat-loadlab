"""Error taxonomy for LibreChat, derived from its middleware and stream code.

Transport errors are only half the story; `silent_*` classes cover failures the
server reports as success (see PLAN.md 2.5).
"""

from __future__ import annotations

from typing import Any

# human labels shown in the dashboard
LABELS: dict[str, str] = {
    "ok": "ok",
    "auth": "auth (401/403)",
    "limiter_message_ip": "429 message/IP",
    "limiter_message_user": "429 message/user",
    "limiter_concurrent": "429 one-message-at-a-time",
    "limiter_upload": "429 upload",
    "limiter_login": "429 login",
    "limiter_register": "429 register",
    "limiter_tool": "429 tool call",
    "limiter_generic": "429 other limiter",
    "validation": "400 validation",
    "server": "5xx server",
    "sse_error": "SSE error event",
    "stream_terminal_error": "generation failed",
    "stream_disconnect": "stream died (client-synthesized)",
    "stream_stall": "stream stalled (no data)",
    "stream_reconcile": "terminal reconcile",
    "abort": "aborted",
    "mcp_tool_error": "MCP tool returned error",
    "silent_meili": "Meili index lag (silent)",
    "silent_rag": "RAG query swallowed (silent)",
    "probe_error": "probe failed",
    "unknown": "unknown",
}

LIMITER_PATHS = {
    "message_ip_limiter": "limiter_message_ip",
    "message_user_limiter": "limiter_message_user",
    "file_upload_ip_limiter": "limiter_upload",
    "file_upload_user_limiter": "limiter_upload",
    "login_limiter": "limiter_login",
    "register_limiter": "limiter_register",
    "tool_call_limiter": "limiter_tool",
}


def _body_text(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "text"):
            value = body.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
        return str(body)[:300]
    if body is None:
        return ""
    return str(body)[:300]


def classify_http(status: int, body: Any = None) -> tuple[str, str | None, str]:
    """Return (error_class, limiter, detail)."""
    detail = _body_text(body)
    if status < 400:
        return "ok", None, detail
    if status == 429:
        payload = body if isinstance(body, dict) else {}
        violation = str(payload.get("type") or payload.get("code") or "")
        if violation == "concurrent":
            return "limiter_concurrent", "concurrency", detail
        if violation:
            return "limiter_generic", violation, detail
        limiter = str(payload.get("limiter") or "")
        if limiter in ("ip", "user") and "file" in detail.lower():
            return "limiter_upload", f"upload/{limiter}", detail
        if "file upload" in detail.lower():
            return "limiter_upload", limiter or "upload", detail
        return LIMITER_PATHS.get(limiter, "limiter_generic"), limiter or "unknown", detail
    if status in (401, 403):
        return "auth", None, detail
    if status == 400 or status == 422:
        return "validation", None, detail
    if status >= 500:
        return "server", None, detail
    return "unknown", None, detail


def classify_final(payload: dict[str, Any]) -> tuple[str, str | None, str]:
    """Classify a terminal `final` SSE payload."""
    status = payload.get("terminalStatus")
    detail = ""
    error = payload.get("error")
    if isinstance(error, dict):
        detail = str(error.get("message") or "")
    elif isinstance(error, str):
        detail = error
    response = payload.get("responseMessage") or {}
    if not detail and isinstance(response, dict) and response.get("error"):
        detail = str(response.get("error"))
    if payload.get("reconcile"):
        return "stream_reconcile", str(payload.get("reconcileReason") or ""), detail
    if status == "error" or response.get("error"):
        return "stream_terminal_error", None, detail
    if status == "aborted" or payload.get("aborted"):
        return "abort", None, detail
    return "ok", None, detail


def is_mcp_tool_error(output: Any) -> bool:
    """MCP/tool failures arrive as tool output text, not HTTP errors."""
    if not isinstance(output, str):
        return False
    lowered = output.lower()
    return (
        "tool call failed" in lowered
        or "error processing tool" in lowered
        or "please fix your mistakes" in lowered
        or lowered.startswith("error:")
    )


def rag_result_swallowed(text: Any) -> bool:
    """`file_search` turns its own HTTP failures into this friendly string."""
    if not isinstance(text, str):
        return False
    return "no results found or errors occurred" in text.lower()
