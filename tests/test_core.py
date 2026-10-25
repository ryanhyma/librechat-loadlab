import pytest

from loadlab import errs
from loadlab.collector import parse_prometheus
from loadlab.store import Sample, Store, pct


def test_http_429_concurrency():
    cls, limiter, _ = errs.classify_http(429, {"type": "concurrent", "limit": 1, "pendingRequests": 1})
    assert cls == "limiter_concurrent"


def test_http_429_message_ip():
    cls, limiter, _ = errs.classify_http(429, {"type": "message_limit", "limiter": "ip", "max": 40})
    assert cls == "limiter_generic" and limiter == "message_limit"


def test_http_429_upload_by_text():
    cls, _, _ = errs.classify_http(429, {"message": "Too many file upload requests. Try again later"})
    assert cls == "limiter_upload"


def test_auth_and_server():
    assert errs.classify_http(401, {})[0] == "auth"
    assert errs.classify_http(403, {})[0] == "auth"
    assert errs.classify_http(500, {})[0] == "server"
    assert errs.classify_http(200, {})[0] == "ok"


def test_final_terminal_states():
    assert errs.classify_final({"terminalStatus": "complete"})[0] == "ok"
    assert errs.classify_final({"terminalStatus": "aborted"})[0] == "abort"
    assert errs.classify_final({"terminalStatus": "error", "error": {"message": "boom"}})[0] == "stream_terminal_error"
    cls, reason, _ = errs.classify_final({"terminalStatus": "error", "reconcile": True, "reconcileReason": "generation_replaced"})
    assert cls == "stream_reconcile" and reason == "generation_replaced"


def test_upstream_429_and_503_are_not_confused_with_local_limiters():
    cls, _, _ = errs.classify_final({"terminalStatus": "error", "error": {"message": "Error code: 429 - rate limit reached for gpt-4o"}})
    assert cls == "provider_429"
    cls, _, _ = errs.classify_final({"terminalStatus": "error", "error": {"message": "503 no slot available"}})
    assert cls == "provider_unavailable"
    assert errs.classify_final({"terminalStatus": "error", "error": {"message": "boom"}})[0] == "stream_terminal_error"


def test_mcp_and_rag_silent_markers():
    assert errs.is_mcp_tool_error("Error: [list_templates] tool call failed: timeout")
    assert not errs.is_mcp_tool_error("all good")
    assert errs.rag_result_swallowed("No results found or errors occurred while searching the files.")
    assert not errs.rag_result_swallowed("42 is the answer")


def test_percentiles():
    assert pct([], 0.95) is None
    assert pct([10], 0.95) == 10.0
    assert pct([1, 2, 3, 4], 0.5) == 2.5
    assert pct(list(range(1, 101)), 0.95) == pytest.approx(95.1, abs=0.2)


def test_prometheus_parse():
    text = """# HELP http_requests_total Total
http_requests_total{method="POST",path="/api/agents/chat/local27b",status="429"} 3
sse_streams_in_flight{method="GET",path="/api/agents/chat/stream/#id"} 2
junk line
"""
    values = parse_prometheus(text)
    assert values['http_requests_total{method="POST",path="/api/agents/chat/local27b",status="429"}'] == 3
    assert values["sse_streams_in_flight"] == 2


def test_store_capacity_and_windows():
    store = Store(users=2, duration_s=10)
    base = store.started_at
    for i in range(4):
        store.add(Sample(base + i, "chat", True, 200, total_ms=100, ttft_ms=50, user=i % 2))
    assert store.snapshot()["totals"]["requests"] == 4
    capacity = store.capacity(slo_p95_ms=200, max_error_rate=0.1)
    assert capacity["capacity_users"] >= 1
    store.add(Sample(base + 5, "chat", False, 429, "limiter_concurrent", "concurrency", total_ms=10, user=1))
    assert store.snapshot()["errors_by_class"]["limiter_concurrent"] == 1
