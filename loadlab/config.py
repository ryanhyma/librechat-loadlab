"""Configuration models. One RunConfig describes everything a run does."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _load_dotenv(path: Path) -> None:
    """Best-effort: reuse LibreChat's .env for secrets we share (never overwrite env)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key.startswith("LOADLAB_") and key not in os.environ:
            os.environ[key] = value
        elif key in ("MEILI_MASTER_KEY", "METRICS_SECRET", "JWT_SECRET") and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(os.environ.get("LOADLAB_ENV_FILE", str(HOME / "dev/librechat/.env"))))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Target(BaseModel):
    """Where LibreChat and its dependencies live."""

    base_url: str = _env("LOADLAB_BASE_URL", "http://localhost:3080")
    mongo_uri: str = _env("LOADLAB_MONGO_URI", "mongodb://172.18.0.4:27017/LibreChat")
    meili_url: str = _env("LOADLAB_MEILI_URL", "http://172.18.0.3:7700")
    meili_key: str = _env("LOADLAB_MEILI_KEY", _env("MEILI_MASTER_KEY", ""))
    rag_url: str = _env("LOADLAB_RAG_URL", "http://172.18.0.5:8000")
    exporter_url: str = _env("LOADLAB_EXPORTER_URL", "")
    metrics_secret: str = _env("LOADLAB_METRICS_SECRET", _env("METRICS_SECRET", ""))
    uploads_dir: str = _env("LOADLAB_UPLOADS_DIR", str(HOME / "dev/librechat/uploads"))
    pg_dsn: str = _env(
        "LOADLAB_PG_DSN", "postgresql://myuser:mypassword@172.18.0.2:5432/mydatabase"
    )
    provider_url: str = _env("LOADLAB_PROVIDER_URL", "")
    provider_interval_s: float = float(_env("LOADLAB_PROVIDER_INTERVAL_S", "5"))
    provider_key: str = _env("LOADLAB_PROVIDER_KEY", "")
    llm_metrics_url: str = _env("LOADLAB_LLM_METRICS_URL", "")
    llm_metrics_key: str = _env("LOADLAB_LLM_METRICS_KEY", _env("LOADLAB_LITELLM_KEY", ""))
    endpoint: str = "local27b"
    model: str = "qwen3.6-27b"
    endpoint_type: Literal["custom", "agents"] = "custom"


class MixItem(BaseModel):
    """One archetype: a weight in the traffic mix plus its params."""

    weight: float = 1.0
    think_ms: int = 1200
    turns: int = 3          # chat turns before starting a new conversation
    prompt: str = "Reply with one short sentence about load testing."
    file_kb: int = 8        # upload size for `rag`
    abort_after_ms: int = 400  # `impatient` abort delay


class RunConfig(BaseModel):
    name: str = "run"
    users: int = Field(5, ge=1, le=5000)
    ramp_s: float = Field(10, ge=0)
    duration_s: float = Field(60, ge=5)
    think_ms: int = Field(800, ge=0)
    mix: dict[str, MixItem] = Field(
        default_factory=lambda: {
            "chatty": MixItem(weight=6, turns=3),
            "reader": MixItem(weight=2),
            "search": MixItem(weight=1),
            "rag": MixItem(weight=1, file_kb=8),
        }
    )
    target: Target = Field(default_factory=Target)
    seed_users: bool = True
    probe_meili: bool = True
    probe_rag: bool = True
    slo_p95_ms: int = 4000
    max_error_rate: float = 0.02

    @field_validator("mix")
    @classmethod
    def _weights(cls, mix: dict[str, MixItem]) -> dict[str, MixItem]:
        if not mix:
            raise ValueError("mix must contain at least one archetype")
        if sum(i.weight for i in mix.values()) <= 0:
            raise ValueError("mix weights must sum to > 0")
        return mix

    @property
    def run_dir(self) -> Path:
        return RUNS_DIR / self.name


def load_config(path: str | Path) -> RunConfig:
    return RunConfig.model_validate(json.loads(Path(path).read_text()))
