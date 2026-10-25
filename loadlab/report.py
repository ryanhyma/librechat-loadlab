"""Run report: capacity verdict, error breakdown, silent-failure probes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import RunConfig
from .errs import LABELS
from .store import Store


def build(cfg: RunConfig, store: Store) -> dict[str, Any]:
    snap = store.snapshot()
    return {
        "generated_at": time.time(),
        "config": cfg.model_dump(),
        "totals": snap["totals"],
        "by_action": snap["by_action"],
        "errors_by_class": snap["errors_by_class"],
        "errors_by_limiter": snap["errors_by_limiter"],
        "windows": store.windows(),
        "capacity": store.capacity(cfg.slo_p95_ms, cfg.max_error_rate),
        "probes": store.probes,
        "host": snap["host"],
        "server": snap["server"],
        "db": snap["db"],
        "recent_errors": snap["recent_errors"],
    }


def write(cfg: RunConfig, store: Store, ledger_dir: Path) -> Path:
    data = build(cfg, store)
    (ledger_dir / "report.json").write_text(json.dumps(data, indent=2, default=str))
    md = markdown(data)
    path = ledger_dir / "report.md"
    path.write_text(md)
    return path


def markdown(data: dict[str, Any]) -> str:
    cfg = data["config"]
    cap = data["capacity"]
    totals = data["totals"]
    lines = [
        f"# loadlab report — {cfg['name']}",
        "",
        f"- users: **{cfg['users']}** · ramp {cfg['ramp_s']}s · duration {cfg['duration_s']}s",
        f"- target: `{cfg['target']['base_url']}` endpoint `{cfg['target']['endpoint']}` "
        f"model `{cfg['target']['model']}`",
        f"- requests: {totals['requests']} · errors {totals['errors']} "
        f"({totals['error_rate'] * 100:.2f}%) · rps(10s) {totals['rps_10s']}",
        "",
        "## Capacity",
        "",
        f"- SLO p95 ≤ {cap['slo_p95_ms']}ms, error rate ≤ {cap['max_error_rate'] * 100:.1f}%",
        f"- **sustained concurrency at SLO: {cap['capacity_users']} users**",
    ]
    if cap["first_breach"]:
        b = cap["first_breach"]
        lines.append(
            f"- first breach: {b['users']} users @ {b['t_s']}s — p95 {b['p95_ms']}ms, "
            f"error rate {b['error_rate'] * 100:.1f}%"
        )
    lines += ["", "## Per action", "", "| action | n | err | p50 | p95 | p99 | ttft p95 |", "|---|---|---|---|---|---|---|"]
    for action, row in data["by_action"].items():
        lines.append(
            f"| {action} | {row['count']} | {row['errors']} | {row['p50_ms']} | {row['p95_ms']} | "
            f"{row['p99_ms']} | {row['ttft_p95_ms']} |"
        )
    lines += ["", "## Errors by class", ""]
    if data["errors_by_class"]:
        for cls, n in data["errors_by_class"].items():
            lines.append(f"- `{cls}` ({LABELS.get(cls, cls)}): {n}")
    else:
        lines.append("- none")
    if data["errors_by_limiter"]:
        lines += ["", "## Errors by limiter", ""]
        for lim, n in data["errors_by_limiter"].items():
            lines.append(f"- `{lim}`: {n}")
    lines += ["", "## Silent-failure probes", ""]
    if data["probes"]:
        lines.append("| probe | ok | latency ms | class | detail |")
        lines.append("|---|---|---|---|---|")
        for p in data["probes"][-40:]:
            lines.append(
                f"| {p['kind']} | {p['ok']} | {p['latency_ms']} | {p['error_class']} | "
                f"{str(p['detail'])[:80]} |"
            )
    else:
        lines.append("- no probes ran")
    lines += [
        "",
        "## Server / host",
        "",
        f"- host: `{json.dumps(data['host'], default=str)}`",
        f"- librechat metrics: `{json.dumps(data['server'], default=str)[:400]}`",
        f"- mongo counters: `{json.dumps(data['db'], default=str)}`",
        "",
        "## Recent errors",
        "",
    ]
    for err in data["recent_errors"][-10:]:
        lines.append(
            f"- {err['action']} · {err['error_class']} · {err.get('limiter') or ''} "
            f"· {str(err.get('detail'))[:120]}"
        )
    lines.append("")
    return "\n".join(lines)
