"""Restart the isolated V10 worker if it exits, stops heartbeating, or stalls."""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import db_path

STATUS_PATH = Path(db_path("direction_shadow_worker_status.json"))
CHECK_SECONDS = 15
STARTUP_GRACE_SECONDS = 60
HEARTBEAT_MAX_AGE = 90
CYCLE_MAX_AGE = 1800
IDLE_MAX_AGE = 1800


def _age(raw):
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (ValueError, TypeError):
        return None


def restart_reason(status, pid, uptime):
    if uptime < STARTUP_GRACE_SECONDS:
        return None
    if not status or status.get("pid") != pid:
        return "missing_current_worker_status"
    heartbeat_age = _age(status.get("heartbeat_at"))
    if heartbeat_age is None or heartbeat_age > HEARTBEAT_MAX_AGE:
        return "stale_heartbeat"
    running = status.get("status") == "RUNNING"
    age = _age(status.get("last_cycle_started_at") if running else status.get("last_cycle_finished_at"))
    if age is None or age > (CYCLE_MAX_AGE if running else IDLE_MAX_AGE):
        return "stalled_cycle"
    return None


def _stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main():
    proc = None

    def shutdown(signum, frame):
        _stop(proc)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while True:
            proc = subprocess.Popen([sys.executable, "direction_shadow_worker.py"])
            launched = time.monotonic()
            print("DIRECTION_SUPERVISOR_START", proc.pid, flush=True)
            while proc.poll() is None:
                time.sleep(CHECK_SECONDS)
                try:
                    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    status = {}
                reason = restart_reason(status, proc.pid, time.monotonic() - launched)
                if reason:
                    print("DIRECTION_SUPERVISOR_RESTART", reason, flush=True)
                    _stop(proc)
                    break
            print("DIRECTION_SUPERVISOR_EXIT", proc.poll(), flush=True)
            time.sleep(5)
    finally:
        _stop(proc)


if __name__ == "__main__":
    main()
