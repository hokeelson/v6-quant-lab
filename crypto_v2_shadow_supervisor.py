from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir

CHECK_SECONDS = 20
STALE_SECONDS = 300
RESTART_DELAY_SECONDS = 5
STATUS_PATH = Path(data_dir()) / "crypto_v2_shadow_worker_status.json"


def _activity_age() -> float | None:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        raw = payload.get("finished_at")
        if not raw:
            return None
        finished = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - finished.astimezone(timezone.utc)).total_seconds(),
        )
    except Exception:
        return None


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

            # Give a newly launched worker enough time to complete its first cycle.
            if time.time() - launched_at < STALE_SECONDS:
                continue

            age = _activity_age()
            if age is not None and age <= STALE_SECONDS:
                continue

            stale_seen = True
            print(
                f"Crypto V2 worker activity stale ({age if age is not None else 'missing'}s); "
                f"restarting pid={proc.pid}",
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
