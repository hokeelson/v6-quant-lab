from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.paths import data_dir
from src.pretrade_risk import write_pretrade_risk_snapshot
from src.pro_risk_engine import write_professional_risk_snapshot

POLL_SECONDS = 60
HEARTBEAT_SECONDS = 15

load_dotenv()
engine = AutoOrchestratorV8(initial_equity=100000.0)
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
    "risk_layer": "STARTING",
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
print("V6 V8 Auto Simulation Worker started: crypto + US stocks + Taiwan stocks.", flush=True)
print("Broker order API = 0. This worker uses virtual accounts only.", flush=True)
print("Professional Risk Layer = shadow-only monitoring.", flush=True)

while True:
    stamp = datetime.now(timezone.utc).isoformat()
    with status_lock:
        worker_state.update({"status": "RUNNING", "last_cycle_started_at": stamp, "message": "Automatic cycle running"})
    _write_status()
    try:
        r = engine.full_cycle()
        sim = r.get("simulation", {}) or {}
        risk_errors = []
        global_risk = "UNKNOWN"
        try:
            risk = write_professional_risk_snapshot(engine.db, engine.cache)
            global_rows = ((risk.get("portfolio") or {}).get("groups") or [])
            global_risk = next((x.get("risk_status") for x in global_rows if x.get("group") == "GLOBAL"), "LOW")
        except Exception as exc:
            risk_errors.append(f"professional: {type(exc).__name__}: {exc}")
            print(stamp, "RISK_LAYER_ERROR", risk_errors[-1], flush=True)
        try:
            write_pretrade_risk_snapshot(engine.db, engine.cache)
        except Exception as exc:
            risk_errors.append(f"pretrade: {type(exc).__name__}: {exc}")
            print(stamp, "PRETRADE_RISK_ERROR", risk_errors[-1], flush=True)

        finished = datetime.now(timezone.utc).isoformat()
        core_errors = len(r.get("true_errors", []) or [])
        with status_lock:
            worker_state.update({
                "status": "ONLINE" if core_errors == 0 and not risk_errors else "DEGRADED",
                "last_cycle_finished_at": finished,
                "assets_checked": int(sim.get("assets_checked", 0) or 0),
                "bars_processed": int(sim.get("bars_processed", 0) or 0),
                "market_data_api_calls": int(sim.get("market_data_api_calls", 0) or 0),
                "broker_order_api_calls": 0,
                "true_errors": core_errors,
                "risk_layer": "ONLINE" if not risk_errors else "ERROR",
                "portfolio_risk": global_risk,
                "message": "Automatic cycle completed" if not risk_errors else "Core cycle completed; " + " | ".join(risk_errors),
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
