from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.data_quality_drift import DataQualityDriftMonitor
from src.paths import data_dir, db_path
from src.pretrade_risk import write_pretrade_risk_snapshot
from src.pro_risk_engine import write_professional_risk_snapshot
from src.realtime_layer import RealtimeDB, build_realtime_watchlist

POLL_SECONDS = 60
HEARTBEAT_SECONDS = 15

load_dotenv()
engine = AutoOrchestratorV8(initial_equity=100000.0)
quality_monitor = DataQualityDriftMonitor(db_path("data_quality.sqlite3"))
realtime_db = RealtimeDB()
status_path = Path(data_dir()) / "worker_status.json"
request_path = Path(data_dir()) / "worker_request.json"
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
    "risk_sizing": "ACTIVE",
    "data_quality": "STARTING",
    "data_quality_warnings": 0,
    "data_quality_critical": 0,
    "concept_drift_pairs": 0,
    "realtime_watchlist_sync": "STARTING",
    "realtime_watchlist_total": 0,
    "last_request_kind": None,
    "last_request_at": None,
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


def _pop_request():
    try:
        if not request_path.exists():
            return None
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        request_path.unlink(missing_ok=True)
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        print("WORKER_REQUEST_ERROR", type(exc).__name__, exc, flush=True)
        return None


def _sleep_until_due():
    # Wake early when the dashboard queues a manual request instead of forcing the
    # user to wait for the next 60-second scheduled cycle.
    for _ in range(POLL_SECONDS):
        if request_path.exists():
            return
        time.sleep(1)


threading.Thread(target=_heartbeat_loop, daemon=True).start()
print("V6 V8 Auto Simulation Worker started: crypto + US stocks + Taiwan stocks.", flush=True)
print("Broker order API = 0. This worker uses virtual accounts only.", flush=True)
print("Professional Risk Layer = active virtual position sizing; no hard trade block.", flush=True)
print("Data Quality + Concept Drift = active sizing guard; cache-only scan, no extra market-data API calls.", flush=True)
print("Realtime watchlist is synchronized by the main worker after every cycle.", flush=True)

while True:
    request = _pop_request()
    request_kind = str((request or {}).get("kind") or "scheduled")
    force_recalibrate = request_kind == "force_calibration"
    stamp = datetime.now(timezone.utc).isoformat()
    with status_lock:
        worker_state.update({
            "status": "RUNNING",
            "last_cycle_started_at": stamp,
            "last_request_kind": request_kind if request else None,
            "last_request_at": (request or {}).get("requested_at"),
            "message": "Manual update running" if request else "Automatic cycle running",
        })
    _write_status()
    try:
        r = engine.full_cycle(force_recalibrate=force_recalibrate)
        sim = r.get("simulation", {}) or {}
        auxiliary_errors = []
        global_risk = "UNKNOWN"
        realtime_watchlist_total = 0
        quality_result = {
            "status": "UNKNOWN", "warnings": 0, "critical_data": 0, "drifted": 0, "errors": []
        }

        # Synchronize the realtime watchlist from the SAME SimulationDB instance
        # that just completed the core cycle. This removes cross-process timing
        # ambiguity: if the core worker sees positions/assets, realtime gets them.
        try:
            realtime_rows = build_realtime_watchlist(engine.db, realtime_db)
            realtime_watchlist_total = len(realtime_rows)
            positions_seen = len(engine.db.positions())
            assets_seen = len(engine.db.assets())
            if realtime_watchlist_total == 0 and (positions_seen > 0 or assets_seen > 0):
                raise RuntimeError(
                    f"watchlist unexpectedly empty; positions={positions_seen}, assets={assets_seen}"
                )
            realtime_sync_status = "ONLINE"
        except Exception as exc:
            realtime_sync_status = "ERROR"
            auxiliary_errors.append(f"realtime_watchlist: {type(exc).__name__}: {exc}")
            print(stamp, "REALTIME_WATCHLIST_SYNC_ERROR", auxiliary_errors[-1], flush=True)

        try:
            risk = write_professional_risk_snapshot(engine.db, engine.cache)
            global_rows = ((risk.get("portfolio") or {}).get("groups") or [])
            global_risk = next((x.get("risk_status") for x in global_rows if x.get("group") == "GLOBAL"), "LOW")
        except Exception as exc:
            auxiliary_errors.append(f"professional: {type(exc).__name__}: {exc}")
            print(stamp, "RISK_LAYER_ERROR", auxiliary_errors[-1], flush=True)
        try:
            write_pretrade_risk_snapshot(engine.db, engine.cache)
        except Exception as exc:
            auxiliary_errors.append(f"pretrade: {type(exc).__name__}: {exc}")
            print(stamp, "PRETRADE_RISK_ERROR", auxiliary_errors[-1], flush=True)

        # Read only the already-populated OHLCV cache. This monitor never fetches
        # data itself, so enabling it does not increase Alpaca/Binance/Yahoo calls.
        try:
            quality_result = quality_monitor.scan_all(engine.db, engine.cache)
            if quality_result.get("errors"):
                auxiliary_errors.append(
                    f"data_quality: {len(quality_result.get('errors') or [])} pair scan errors"
                )
                print(stamp, "DATA_QUALITY_PARTIAL", quality_result.get("errors"), flush=True)
        except Exception as exc:
            quality_result = {
                "status": "ERROR", "warnings": 0, "critical_data": 0, "drifted": 0,
                "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
            auxiliary_errors.append(f"data_quality: {type(exc).__name__}: {exc}")
            print(stamp, "DATA_QUALITY_ERROR", auxiliary_errors[-1], flush=True)

        finished = datetime.now(timezone.utc).isoformat()
        core_errors = len(r.get("true_errors", []) or [])
        with status_lock:
            worker_state.update({
                "status": "ONLINE" if core_errors == 0 and not auxiliary_errors else "DEGRADED",
                "last_cycle_finished_at": finished,
                "assets_checked": int(sim.get("assets_checked", 0) or 0),
                "bars_processed": int(sim.get("bars_processed", 0) or 0),
                "market_data_api_calls": int(sim.get("market_data_api_calls", 0) or 0),
                "broker_order_api_calls": 0,
                "true_errors": core_errors,
                "risk_layer": "ONLINE" if not any(x.startswith(("professional:", "pretrade:")) for x in auxiliary_errors) else "ERROR",
                "risk_sizing": "ACTIVE",
                "data_quality": str(quality_result.get("status") or "UNKNOWN"),
                "data_quality_warnings": int(quality_result.get("warnings", 0) or 0),
                "data_quality_critical": int(quality_result.get("critical_data", 0) or 0),
                "concept_drift_pairs": int(quality_result.get("drifted", 0) or 0),
                "portfolio_risk": global_risk,
                "realtime_watchlist_sync": realtime_sync_status,
                "realtime_watchlist_total": realtime_watchlist_total,
                "message": "Manual update completed" if request and not auxiliary_errors else (
                    "Automatic cycle completed" if not auxiliary_errors else "Core cycle completed; " + " | ".join(auxiliary_errors)
                ),
            })
        _write_status()
        print(stamp, request_kind, r, "DATA_QUALITY", quality_result, flush=True)
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
    _sleep_until_due()
