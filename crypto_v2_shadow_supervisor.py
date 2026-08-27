from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir

CHECK_SECONDS = 20
MISSING_STATUS_GRACE_SECONDS = 180
IDLE_STALE_SECONDS = 300
MAX_CYCLE_SECONDS = 3600
RESTART_DELAY_SECONDS = 5
STATUS_PATH = Path(data_dir()) / "crypto_v2_shadow_worker_status.json"


def _age_seconds(raw) -> float | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds(),
        )
    except Exception:
        return None


def _read_status() -> dict | None:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _restart_reason(launched_at: float) -> str | None:
    # The worker now writes RUNNING before each cycle. A catch-up cycle can be
    # substantially longer than five minutes, so never treat an active cycle as
    # stale merely because the previous completed snapshot is old.
    payload = _read_status()
    if payload is None:
        if time.time() - launched_at <= MISSING_STATUS_GRACE_SECONDS:
            return None
        return "worker status missing"

    status = str(payload.get("status") or "").upper()
    if status == "RUNNING":
        age = _age_seconds(payload.get("started_at"))
        if age is None:
            return "RUNNING status missing started_at"
        if age <= MAX_CYCLE_SECONDS:
            return None
        return f"cycle exceeded {MAX_CYCLE_SECONDS}s ({age:.1f}s)"

    age = _age_seconds(payload.get("finished_at"))
    if age is not None and age <= IDLE_STALE_SECONDS:
        return None
    return f"completed activity stale ({age if age is not None else 'missing'}s)"


def _stop(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    print("Crypto V2 Shadow Supervisor started.", flush=True)
    while True:
        launched_at = time.time()
        proc = subprocess.Popen([sys.executable, "crypto_v2_shadow_worker.py"])
        print(f"Crypto V2 worker launched pid={proc.pid}", flush=True)
        stale_seen = False

        while proc.poll() is None:
            time.sleep(CHECK_SECONDS)
            reason = _restart_reason(launched_at)
            if reason is None:
                continue

            stale_seen = True
            print(
                f"Crypto V2 worker unhealthy: {reason}; restarting pid={proc.pid}",
                flush=True,
            )
            _stop(proc)
            break

        code = proc.poll()
        if not stale_seen:
            print(
                f"Crypto V2 worker exited code={code}; restarting in {RESTART_DELAY_SECONDS}s",
                flush=True,
            )
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
