"""In-memory run telemetry: samples, probes, live snapshots, percentiles."""

from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Sample:
    t: float
    action: str
    ok: bool
    status: int
    error_class: str = "ok"
    limiter: str | None = None
    detail: str = ""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    events: int = 0
    user: int = 0
    conversation_id: str | None = None
    chars: int = 0


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return round(data[0], 1)
    pos = (len(data) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return round(data[lo], 1)
    return round(data[lo] + (data[hi] - data[lo]) * (pos - lo), 1)


class Store:
    def __init__(self, users: int, duration_s: float) -> None:
        self.started_at = time.time()
        self.duration_s = duration_s
        self.users_total = users
        self.samples: list[Sample] = []
        self.probes: list[dict[str, Any]] = []
        self.users_active = 0
        self.in_flight: Counter[str] = Counter()
        self.errors_by_class: Counter[str] = Counter()
        self.errors_by_limiter: Counter[str] = Counter()
        self.action_counts: Counter[str] = Counter()
        self.action_ok: Counter[str] = Counter()
        self.host: deque[dict[str, Any]] = deque(maxlen=600)
        self.server: deque[dict[str, Any]] = deque(maxlen=600)
        self.db: deque[dict[str, Any]] = deque(maxlen=600)
        self.finished = False
        self.stop_reason = ""

    # ------------------------------------------------------------- record
    def add(self, sample: Sample) -> None:
        self.samples.append(sample)
        self.action_counts[sample.action] += 1
        if sample.ok:
            self.action_ok[sample.action] += 1
        else:
            self.errors_by_class[sample.error_class] += 1
            if sample.limiter:
                self.errors_by_limiter[sample.limiter] += 1

    def add_probe(self, kind: str, ok: bool, latency_ms: float | None, error_class: str, detail: str = "") -> None:
        self.probes.append(
            {
                "t": time.time(),
                "kind": kind,
                "ok": ok,
                "latency_ms": round(latency_ms, 1) if latency_ms else None,
                "error_class": error_class,
                "detail": detail[:300],
            }
        )
        self.action_counts[f"probe:{kind}"] += 1
        if ok:
            self.action_ok[f"probe:{kind}"] += 1
        else:
            self.errors_by_class[error_class] += 1

    def enter(self, action: str) -> None:
        self.in_flight[action] += 1

    def leave(self, action: str) -> None:
        self.in_flight[action] -= 1
        if self.in_flight[action] <= 0:
            del self.in_flight[action]

    # ------------------------------------------------------------- views
    @property
    def elapsed(self) -> float:
        return round(time.time() - self.started_at, 1)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        recent = [s for s in self.samples if now - s.t <= 10]
        by_action: dict[str, dict[str, Any]] = {}
        grouped: dict[str, list[Sample]] = defaultdict(list)
        for s in self.samples:
            grouped[s.action].append(s)
        for action, rows in sorted(grouped.items()):
            ok_rows = [r for r in rows if r.ok]
            totals = [r.total_ms for r in rows]
            ttfts = [r.ttft_ms for r in ok_rows if r.ttft_ms is not None]
            by_action[action] = {
                "count": len(rows),
                "ok": len(ok_rows),
                "errors": len(rows) - len(ok_rows),
                "p50_ms": pct(totals, 0.5),
                "p95_ms": pct(totals, 0.95),
                "p99_ms": pct(totals, 0.99),
                "ttft_p50_ms": pct(ttfts, 0.5),
                "ttft_p95_ms": pct(ttfts, 0.95),
            }
        total = len(self.samples)
        errors = sum(1 for s in self.samples if not s.ok)
        return {
            "run": {
                "started_at": self.started_at,
                "elapsed_s": self.elapsed,
                "duration_s": self.duration_s,
                "users_total": self.users_total,
                "users_active": self.users_active,
                "finished": self.finished,
                "stop_reason": self.stop_reason,
            },
            "totals": {
                "requests": total,
                "errors": errors,
                "error_rate": round(errors / total, 4) if total else 0.0,
                "rps_10s": round(len(recent) / 10, 2),
                "inflight": dict(self.in_flight),
                "inflight_total": sum(self.in_flight.values()),
            },
            "by_action": by_action,
            "errors_by_class": dict(self.errors_by_class.most_common()),
            "errors_by_limiter": dict(self.errors_by_limiter.most_common()),
            "recent_errors": [
                asdict(s) for s in self.samples if not s.ok
            ][-25:],
            "probes": self.probes[-40:],
            "host": self.host[-1] if self.host else {},
            "server": self.server[-1] if self.server else {},
            "db": self.db[-1] if self.db else {},
        }

    def windows(self, bucket_s: int = 5) -> list[dict[str, Any]]:
        """Per-window throughput/latency vs concurrent users — the knee input."""
        if not self.samples:
            return []
        t0 = self.started_at
        buckets: dict[int, list[Sample]] = defaultdict(list)
        for s in self.samples:
            buckets[int((s.t - t0) // bucket_s)].append(s)
        out = []
        for idx in sorted(buckets):
            rows = buckets[idx]
            totals = [r.total_ms for r in rows]
            ok = sum(1 for r in rows if r.ok)
            # users actually observed in the interval (proxy: distinct VU ids)
            users = len({r.user for r in rows}) or 1
            out.append(
                {
                    "t_s": round(idx * bucket_s, 1),
                    "users": users,
                    "rps": round(len(rows) / bucket_s, 2),
                    "p95_ms": pct(totals, 0.95),
                    "error_rate": round((len(rows) - ok) / len(rows), 4) if rows else 0.0,
                }
            )
        return out

    def capacity(self, slo_p95_ms: int, max_error_rate: float) -> dict[str, Any]:
        """Largest observed concurrency that met the SLO, plus first breach."""
        good = [
            w
            for w in self.windows()
            if (w["p95_ms"] or 0) <= slo_p95_ms and w["error_rate"] <= max_error_rate
        ]
        breach = next(
            (
                w
                for w in self.windows()
                if (w["p95_ms"] or 0) > slo_p95_ms or w["error_rate"] > max_error_rate
            ),
            None,
        )
        return {
            "slo_p95_ms": slo_p95_ms,
            "max_error_rate": max_error_rate,
            "capacity_users": max((w["users"] for w in good), default=0),
            "first_breach": breach,
        }


def sleep_jitter(base_ms: int, spread: float = 0.4) -> float:
    return max(0.0, base_ms * (1 + random.uniform(-spread, spread))) / 1000
