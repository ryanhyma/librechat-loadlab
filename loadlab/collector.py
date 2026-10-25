"""Background collection: LibreChat /metrics, Mongo counters, host stats."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import httpx
import psutil
from pymongo import MongoClient

from .config import UA, RunConfig
from .store import Store

WANTED = (
    "http_requests_total",
    "http_request_duration_seconds_count",
    "http_requests_in_flight",
    "sse_streams_total",
    "sse_streams_in_flight",
    "sse_stream_duration_seconds_count",
    "upload_requests_total",
    "upload_bytes_total",
    "mongoose_queries_total",
    "generation_jobs_total",
    "generation_jobs_in_flight",
    "generation_stream_recoveries_total",
    "generation_stream_early_buffer_overflows_total",
)


def parse_prometheus(text: str) -> dict[str, Any]:
    """Flatten the counters we care about; keep label tuples for 429s."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            name, value = line.rsplit(" ", 1)
            metric = name.split("{", 1)[0]
            if metric not in WANTED:
                continue
            out[name] = float(value)
            out[metric] = out.get(metric, 0.0) + float(value)
        except ValueError:
            continue
    return out


class Collector:
    def __init__(self, cfg: RunConfig, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.mongo = MongoClient(cfg.target.mongo_uri, serverSelectionTimeoutMS=5000)
        self.db = self.mongo.get_default_database()
        self.started = dt.datetime.now(dt.timezone.utc)

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": UA}) as http:
            while not stop.is_set():
                await asyncio.gather(
                    self._host(), self._metrics(http), self._db(), self._llm_metrics(http),
                    return_exceptions=True,
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

    async def _host(self) -> None:
        self.store.host.append(
            {
                "t": time.time(),
                "cpu": psutil.cpu_percent(interval=None),
                "mem": round(psutil.virtual_memory().percent, 1),
                "disk": round(psutil.disk_usage("/").percent, 1),
            }
        )

    async def _llm_metrics(self, http: httpx.AsyncClient) -> None:
        """Gateway (LiteLLM) counters: the only place a provider 429 is counted per status."""
        if not self.cfg.target.llm_metrics_url:
            return
        headers = {}
        if self.cfg.target.llm_metrics_key:
            headers["Authorization"] = f"Bearer {self.cfg.target.llm_metrics_key}"
        try:
            r = await http.get(self.cfg.target.llm_metrics_url, headers=headers, follow_redirects=True)
        except httpx.HTTPError:
            return
        if r.status_code != 200:
            return
        failed_429 = failed_other = total = 0.0
        for line in r.text.splitlines():
            if line.startswith("litellm_proxy_failed_requests_metric_total{"):
                value = float(line.rsplit(" ", 1)[1])
                if 'exception_status="429"' in line:
                    failed_429 += value
                else:
                    failed_other += value
            elif line.startswith("litellm_proxy_total_requests_metric_total{"):
                total += float(line.rsplit(" ", 1)[1])
        self.store.server.append(
            {"t": time.time(), "llm_requests": total, "llm_failed_429": failed_429, "llm_failed_other": failed_other}
        )

    async def _metrics(self, http: httpx.AsyncClient) -> None:
        if not self.cfg.target.metrics_secret:
            return
        try:
            r = await http.get(
                f"{self.cfg.target.base_url}/metrics",
                headers={"Authorization": f"Bearer {self.cfg.target.metrics_secret}"},
            )
        except httpx.HTTPError:
            return
        if r.status_code != 200:
            self.store.server.append({"t": time.time(), "metrics_status": r.status_code})
            return
        values = parse_prometheus(r.text)
        row: dict[str, Any] = {
            "t": time.time(),
            "metrics_status": 200,
            "sse_in_flight": values.get("sse_streams_in_flight", 0),
            "http_in_flight": values.get("http_requests_in_flight", 0),
            "http_4xx": sum(v for k, v in values.items() if 'http_requests_total{' in k and 'status="4' in k),
            "http_5xx": sum(v for k, v in values.items() if 'http_requests_total{' in k and 'status="5' in k),
            "mongoose_queries": values.get("mongoose_queries_total", 0),
            "generation_recoveries": values.get("generation_stream_recoveries_total", 0),
            "early_buffer_overflows": values.get(
                "generation_stream_early_buffer_overflows_total", 0
            ),
        }
        self.store.server.append(row)

    async def _db(self) -> None:
        since = self.started
        counts = await asyncio.to_thread(self._counts, since)
        counts.update({"t": time.time()})
        self.store.db.append(counts)

    def _counts(self, since: dt.datetime) -> dict[str, Any]:
        try:
            return {
                "messages": self.db.messages.count_documents({"createdAt": {"$gte": since}}),
                "messages_error": self.db.messages.count_documents(
                    {"createdAt": {"$gte": since}, "error": {"$ne": False}}
                ),
                "conversations": self.db.conversations.count_documents(
                    {"createdAt": {"$gte": since}}
                ),
                "files": self.db.files.count_documents({"createdAt": {"$gte": since}}),
                "transactions": self.db.transactions.count_documents(
                    {"createdAt": {"$gte": since}}
                ),
            }
        except Exception:
            return {}
