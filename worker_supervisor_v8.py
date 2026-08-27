from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir

CHECK_SECONDS = 20
HEARTBEAT_STALE_SECONDS = 240
MAX_CYCLE_SECONDS = 900
MISSING_STATUS_GRACE_SECONDS = 240
RESTART_DELAY_SECONDS = 5
STATUS_PATH = Path(data_dir()) / "worker_status.json"


def _age_seconds(raw) -> float | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _read_status() -> dict | None:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _restart_reason(launched_at: float) -> str | None:
    payload = _read_status()
    if payload is None:
        if time.time() - launched_at <= MISSING_STATUS_GRACE_SECONDS:
            return None
        return "worker status missing"

    heartbeat_age = _age_seconds(payload.get("heartbeat_at"))
    if heartbeat_age is None:
        if time.time() - launched_at <= MISSING_STATUS_GRACE_SECONDS:
            return None
        return "worker heartbeat missing"
    if heartbeat_age > HEARTBEAT_STALE_SECONDS:
        return f"worker heartbeat stale ({heartbeat_age:.1f}s)"

    # live_worker_v8 writes heartbeat from a dedicated thread. A blocked
    # full_cycle can therefore keep heartbeat_at perfectly fresh forever.
    # Treat an excessively long RUNNING cycle as hung even when heartbeat lives.
    if str(payload.get("status") or "").upper() == "RUNNING":
        cycle_age = _age_seconds(payload.get("last_cycle_started_at"))
        if cycle_age is None:
            return "RUNNING cycle missing last_cycle_started_at"
        if cycle_age > MAX_CYCLE_SECONDS:
            return f"cycle exceeded {MAX_CYCLE_SECONDS}s ({cycle_age:.1f}s)"

    return None


def _stop(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    print("V6 V8 Worker Supervisor started.", flush=True)
    while True:
        launched_at = time.time()
        proc = subprocess.Popen([sys.executable, "live_worker_v8.py"])
        print(f"V8 worker launched pid={proc.pid}", flush=True)
        restart_seen = False
        while proc.poll() is None:
            time.sleep(CHECK_SECONDS)
            reason = _restart_reason(launched_at)
            if reason is None:
                continue
            restart_seen = True
            print(f"V8 worker unhealthy: {reason}; restarting pid={proc.pid}", flush=True)
            _stop(proc)
            break
        code = proc.poll()
        if not restart_seen:
            print(f"Worker exited code={code}; restarting in {RESTART_DELAY_SECONDS}s", flush=True)
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
