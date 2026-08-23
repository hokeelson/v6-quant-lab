from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir

CHECK_SECONDS = 20
STALE_SECONDS = 240
RESTART_DELAY_SECONDS = 5
STATUS_PATH = Path(data_dir()) / "worker_status.json"


def _heartbeat_age() -> float | None:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        raw = payload.get("heartbeat_at")
        if not raw:
            return None
        hb = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - hb.astimezone(timezone.utc)).total_seconds())
    except Exception:
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
        started_at = time.time()
        proc = subprocess.Popen([sys.executable, "live_worker_v8.py"])
        print(f"V8 worker launched pid={proc.pid}", flush=True)
        stale_seen = False
        while proc.poll() is None:
            time.sleep(CHECK_SECONDS)
            age = _heartbeat_age()
            if age is None and time.time() - started_at < STALE_SECONDS:
                continue
            if age is not None and age <= STALE_SECONDS:
                continue
            stale_seen = True
            print(f"Worker heartbeat stale ({age if age is not None else 'missing'}s); restarting pid={proc.pid}", flush=True)
            _stop(proc)
            break
        code = proc.poll()
        if not stale_seen:
            print(f"Worker exited code={code}; restarting in {RESTART_DELAY_SECONDS}s", flush=True)
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
