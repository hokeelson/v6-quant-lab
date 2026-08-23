from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir

CHECK_SECONDS = 10
STALE_SECONDS = 30
RESTART_DELAY_SECONDS = 3
STATUS_PATH = Path(data_dir()) / "tca_status.json"


def _heartbeat_age():
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


def _stop(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    print("V6 TCA Supervisor started.", flush=True)
    while True:
        started = time.time()
        proc = subprocess.Popen([sys.executable, "tca_worker.py"])
        print(f"TCA worker launched pid={proc.pid}", flush=True)
        stale = False
        while proc.poll() is None:
            time.sleep(CHECK_SECONDS)
            age = _heartbeat_age()
            if age is None and time.time() - started < STALE_SECONDS:
                continue
            if age is not None and age <= STALE_SECONDS:
                continue
            stale = True
            print(f"TCA heartbeat stale ({age if age is not None else 'missing'}s); restarting", flush=True)
            _stop(proc)
            break
        if not stale:
            print(f"TCA worker exited code={proc.poll()}; restarting", flush=True)
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
