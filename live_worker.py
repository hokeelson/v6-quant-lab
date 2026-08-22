from __future__ import annotations
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from src.auto_orchestrator import AutoOrchestrator
from src.paths import data_dir

POLL_SECONDS = 60
HEARTBEAT_SECONDS = 15

load_dotenv()
engine = AutoOrchestrator(initial_equity=100000.0)
status_path = Path(data_dir()) / "worker_status.json"
status_lock = threading.Lock()
worker_state = {
    "status": "STARTING",
    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "assets_checked": 0,
    "bars_processed": 0,
    "market_data_api_calls": 0,
    "broker_order_api_calls": 0,
    "true_errors": 0,
    "message": "Worker starting",
}


def _write_status():
    with status_lock:
        worker_state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        payload = dict(worker_state)
    tmp = status_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(status_path)


def _heartbeat_loop():
    while True:
        try:
            _write_status()
        except Exception as e:
            print("HEARTBEAT_ERROR", type(e).__name__, e, flush=True)
        time.sleep(HEARTBEAT_SECONDS)


threading.Thread(target=_heartbeat_loop, daemon=True).start()
print("V6 Stage 7 Auto Simulation Worker started.")
print("Broker order API = 0. Market-data API is throttled and cached locally.")
print("Dashboard reads SQLite only; it does not trigger API calls every refresh.")

while True:
    stamp = datetime.now(timezone.utc).isoformat()
    with status_lock:
        worker_state.update({
            "status": "RUNNING",
            "last_cycle_started_at": stamp,
            "message": "Automatic cycle running",
        })
    _write_status()
    try:
        r = engine.full_cycle()
        sim = r.get("simulation", {}) or {}
        finished = datetime.now(timezone.utc).isoformat()
        with status_lock:
            worker_state.update({
                "status": "ONLINE" if not (r.get("true_errors") or []) else "DEGRADED",
                "last_cycle_finished_at": finished,
                "assets_checked": int(sim.get("assets_checked", 0) or 0),
                "bars_processed": int(sim.get("bars_processed", 0) or 0),
                "market_data_api_calls": int(sim.get("market_data_api_calls", 0) or 0),
                "broker_order_api_calls": 0,
                "true_errors": len(r.get("true_errors", []) or []),
                "message": "Automatic cycle completed",
            })
        _write_status()
        print(stamp, r, flush=True)
    except Exception as e:
        finished = datetime.now(timezone.utc).isoformat()
        with status_lock:
            worker_state.update({
                "status": "ERROR",
                "last_cycle_finished_at": finished,
                "true_errors": 1,
                "message": f"{type(e).__name__}: {e}",
            })
        _write_status()
        print(stamp, "AUTO_ERROR", type(e).__name__, e, flush=True)
    time.sleep(POLL_SECONDS)
