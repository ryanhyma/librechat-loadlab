"""Change-managed test-profile `.env` apply/restore (PLAN.md 5.2).

Prod has no containers: applying the test profile is a file edit plus a service
restart, so the exact bytes are backed up, hashed, and restored with proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import ROOT

STATE = ROOT / "runs" / ".env-state.json"

# values that make a capacity run possible; everything else is left untouched
PROFILE = {
    "LOGIN_MAX": "100000",
    "LOGIN_WINDOW": "1",
    "REGISTER_MAX": "100000",
    "REGISTER_WINDOW": "1",
    "MESSAGE_IP_MAX": "100000",
    "MESSAGE_USER_MAX": "100000",
    "FILE_UPLOAD_IP_MAX": "100000",
    "FILE_UPLOAD_USER_MAX": "100000",
    "METRICS_SECRET": os.environ.get("LOADLAB_METRICS_SECRET", "loadlab-metrics"),
}
# kept as production has it, deliberately measured rather than disabled
KEEP = ("LIMIT_CONCURRENT_MESSAGES", "LIMIT_MESSAGE_IP", "LIMIT_MESSAGE_USER", "SEARCH")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


def _restart() -> tuple[bool, str]:
    cmd = os.environ.get("LOADLAB_RESTART_CMD", "")
    if not cmd:
        return False, "no LOADLAB_RESTART_CMD set; restart LibreChat manually"
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-400:]


def apply(env_file: Path, apply_restart: bool = True) -> dict:
    env_file = env_file.expanduser().resolve()
    if not env_file.exists():
        raise FileNotFoundError(env_file)
    if STATE.exists():
        raise RuntimeError("a profile is already applied; run `loadlab env restore` first")
    backup = env_file.with_suffix(".loadlab.bak")
    shutil.copy2(env_file, backup)
    before = _sha(env_file)
    _write_env(env_file, PROFILE)
    after = _sha(env_file)
    restarted, output = _restart() if apply_restart else (False, "restart skipped")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "env_file": str(env_file),
                "backup": str(backup),
                "before_sha": before,
                "after_sha": after,
                "applied_at": time.time(),
                "profile": PROFILE,
                "restarted": restarted,
                "restart_output": output,
            },
            indent=2,
        )
    )
    return json.loads(STATE.read_text())


def restore(apply_restart: bool = True) -> dict:
    if not STATE.exists():
        raise RuntimeError("no applied profile")
    state = json.loads(STATE.read_text())
    env_file = Path(state["env_file"])
    backup = Path(state["backup"])
    shutil.copy2(backup, env_file)
    now = _sha(env_file)
    restarted, output = _restart() if apply_restart else (False, "restart skipped")
    result = {
        "env_file": str(env_file),
        "restored_sha": now,
        "matches_before": now == state["before_sha"],
        "restarted": restarted,
        "restart_output": output,
    }
    if result["matches_before"]:
        STATE.unlink(missing_ok=True)
    return result


def guard(max_minutes: float = 120.0, apply_restart: bool = True) -> dict:
    """Scheduled watchdog: restore an applied profile left behind by a crashed run."""
    state = status()
    if not state.get("applied"):
        return {"applied": False, "restored": False}
    age_min = (time.time() - float(state["applied_at"])) / 60
    if age_min < max_minutes:
        return {"applied": True, "restored": False, "age_min": round(age_min, 1)}
    return {"applied": True, "restored": True, "age_min": round(age_min, 1), **restore(apply_restart)}


def status() -> dict:
    if not STATE.exists():
        return {"applied": False}
    return {"applied": True, **json.loads(STATE.read_text())}
