from __future__ import annotations

import json
import os
import re
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
from src.status_file import atomic_write_json
from src.worker_progress import CycleProgress

POLL_SECONDS = 60
HEARTBEAT_SECONDS = 15

load_dotenv()
engine = AutoOrchestratorV8(initial_equity=100000.0)
quality_monitor = DataQualityDriftMonitor(db_path("data_quality.sqlite3"))
realtime_db = RealtimeDB()
status_path = Path(data_dir()) / "worker_status.json"
request_path = Path(data_dir()) / "worker_request.json"
status_lock = threading.Lock()
cycle_progress = CycleProgress()
worker_state = {
    "pid": os.getpid(),
    "worker_started_at": datetime.now(timezone.utc).isoformat(),
    "status": "STARTING",
    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "first_cycle_complete": False,
    "assets_checked": 0,
    "bars_processed": 0,
    "market_data_api_calls": 0,
    "broker_order_api_calls": 0,
    "true_errors": 0,
    "true_error_details": [],
    "waiting_data": 0,
    "waiting_data_details": [],
    "auxiliary_error_details": [],
    "risk_layer": "STARTING",
    "risk_sizing": "ACTIVE",
    "data_quality": "STARTING",
    "data_quality_warnings": 0,
    "data_quality_critical": 0,
    "concept_drift_watch": 0,
    "concept_drift_pairs": 0,
    "realtime_watchlist_sync": "STARTING",
    "realtime_watchlist_total": 0,
    "last_request_kind": None,
    "last_request_at": None,
    "message": "Worker starting",
}


def _compact_errors(rows, limit=12):
    out = []
    for row in list(rows or [])[:limit]:
        if isinstance(row, dict):
            item = {}
            for key in ("market", "symbol", "horizon", "error"):
                value = row.get(key)
                if value is not None:
                    item[key] = str(value)[:500]
            out.append(item or {"error": str(row)[:500]})
        else:
            out.append({"error": str(row)[:500]})
    return out


def _is_waiting_data_error(row) -> bool:
    if isinstance(row, dict):
        text = str(row.get("error") or "")
    else:
        text = str(row or "")
    return bool(
        re.search(r"need at least\s+\d+\s+closed bars", text, flags=re.I)
        or re.search(r"not enough bars", text, flags=re.I)
        or re.search(r"insufficient (?:history|bars|data)", text, flags=re.I)
    )


def _split_core_errors(rows):
    true_errors = []
    waiting_data = []
    for row in list(rows or []):
        (waiting_data if _is_waiting_data_error(row) else true_errors).append(row)
    return true_errors, waiting_data


def _write_status():
    with status_lock:
        worker_state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        payload = dict(worker_state)
        payload.update(cycle_progress.snapshot())
    try:
        atomic_write_json(status_path, payload)
        return True
    except Exception as exc:
        # Status telemetry must never kill the simulation worker. The supervisor
        # can still restart a genuinely stale worker if writes remain broken.
        print("WORKER_STATUS_WRITE_ERROR", type(exc).__name__, exc, flush=True)
        return False


def _report_progress(phase, **details):
    with status_lock:
        cycle_progress.report(phase, **details)
        metrics = details.get("metrics") or {}
        for key in ("assets_checked", "bars_processed", "market_data_api_calls"):
            if key in metrics:
                worker_state[key] = int(metrics[key])
    try:
        _write_status()
    except Exception as exc:
        print("WORKER_PROGRESS_ERROR", type(exc).__name__, flush=True)


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
print("Professional Risk Layer = ENTRY_GATE_V1; fail-closed BUY admission, exits unchanged.", flush=True)
print("Data Quality + Concept Drift = active sizing guard; cache-only scan, no extra market-data API calls.", flush=True)
print("Realtime watchlist is synchronized by the main worker after every cycle.", flush=True)

while True:
    request = _pop_request()
    request_kind = str((request or {}).get("kind") or "scheduled")
    force_recalibrate = request_kind == "force_calibration"
    stamp = datetime.now(timezone.utc).isoformat()
    with status_lock:
        cycle_progress.start(stamp)
        worker_state.update({
            "status": "RUNNING",
            "last_cycle_started_at": stamp,
            "assets_checked": 0,
            "bars_processed": 0,
            "market_data_api_calls": 0,
            "last_request_kind": request_kind if request else None,
            "last_request_at": (request or {}).get("requested_at"),
            "message": "Manual update running" if request else "Automatic cycle running",
        })
    _write_status()
    try:
        r = engine.full_cycle(force_recalibrate=force_recalibrate, progress=_report_progress)
        sim = r.get("simulation", {}) or {}
        auxiliary_errors = []
        global_risk = "UNKNOWN"
        realtime_watchlist_total = 0
        quality_result = {
            "status": "UNKNOWN", "warnings": 0, "drift_watch": 0, "critical_data": 0, "drifted": 0, "errors": []
        }

        # Synchronize the realtime watchlist from the SAME SimulationDB instance
        # that just completed the core cycle. This removes cross-process timing
        # ambiguity: if the core worker sees positions/assets, realtime gets them.
        try:
            _report_progress("WATCHLIST")
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
            _report_progress("PORTFOLIO_RISK")
            risk = write_professional_risk_snapshot(engine.db, engine.cache)
            global_rows = ((risk.get("portfolio") or {}).get("groups") or [])
            global_risk = next((x.get("risk_status") for x in global_rows if x.get("group") == "GLOBAL"), "LOW")
        except Exception as exc:
            auxiliary_errors.append(f"professional: {type(exc).__name__}: {exc}")
            print(stamp, "RISK_LAYER_ERROR", auxiliary_errors[-1], flush=True)
        try:
            _report_progress("PRETRADE_RISK")
            write_pretrade_risk_snapshot(engine.db, engine.cache)
        except Exception as exc:
            auxiliary_errors.append(f"pretrade: {type(exc).__name__}: {exc}")
            print(stamp, "PRETRADE_RISK_ERROR", auxiliary_errors[-1], flush=True)

        # Read only the already-populated OHLCV cache. This monitor never fetches
        # data itself, so enabling it does not increase Alpaca/Binance/Yahoo calls.
        try:
            _report_progress("DATA_QUALITY")
            quality_result = quality_monitor.scan_all(engine.db, engine.cache)
            if quality_result.get("errors"):
                auxiliary_errors.append(
                    f"data_quality: {len(quality_result.get('errors') or [])} pair scan errors"
                )
                print(stamp, "DATA_QUALITY_PARTIAL", quality_result.get("errors"), flush=True)
        except Exception as exc:
            quality_result = {
                "status": "ERROR", "warnings": 0, "drift_watch": 0, "critical_data": 0, "drifted": 0,
                "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
            auxiliary_errors.append(f"data_quality: {type(exc).__name__}: {exc}")
            print(stamp, "DATA_QUALITY_ERROR", auxiliary_errors[-1], flush=True)

        finished = datetime.now(timezone.utc).isoformat()
        raw_core_error_rows = r.get("true_errors", []) or []
        core_error_rows, waiting_data_rows = _split_core_errors(raw_core_error_rows)
        core_errors = len(core_error_rows)
        with status_lock:
            cycle_progress.finish()
            worker_state.update({
                "status": "ONLINE" if core_errors == 0 and not auxiliary_errors else "DEGRADED",
                "last_cycle_finished_at": finished,
                "first_cycle_complete": True,
                "assets_checked": int(sim.get("assets_checked", 0) or 0),
                "bars_processed": int(sim.get("bars_processed", 0) or 0),
                "market_data_api_calls": int(sim.get("market_data_api_calls", 0) or 0),
                "broker_order_api_calls": 0,
                "true_errors": core_errors,
                "true_error_details": _compact_errors(core_error_rows),
                "waiting_data": len(waiting_data_rows),
                "waiting_data_details": _compact_errors(waiting_data_rows),
                "auxiliary_error_details": [{"error": str(x)[:500]} for x in auxiliary_errors[:12]],
                "risk_layer": "ONLINE" if not any(x.startswith(("professional:", "pretrade:")) for x in auxiliary_errors) else "ERROR",
                "risk_sizing": "ACTIVE",
                "data_quality": str(quality_result.get("status") or "UNKNOWN"),
                "data_quality_warnings": int(quality_result.get("warnings", 0) or 0),
                "data_quality_critical": int(quality_result.get("critical_data", 0) or 0),
                "concept_drift_watch": int(quality_result.get("drift_watch", 0) or 0),
                "concept_drift_pairs": int(quality_result.get("drifted", 0) or 0),
                "portfolio_risk": global_risk,
                "realtime_watchlist_sync": realtime_sync_status,
                "realtime_watchlist_total": realtime_watchlist_total,
                "message": "Manual update completed" if request and not auxiliary_errors else (
                    "Automatic cycle completed" if not auxiliary_errors else "Core cycle completed; " + " | ".join(auxiliary_errors)
                ),
            })
        _write_status()
        if waiting_data_rows:
            print(stamp, "WAITING_DATA", _compact_errors(waiting_data_rows), flush=True)
        print(stamp, request_kind, r, "DATA_QUALITY", quality_result, flush=True)
    except Exception as e:
        finished = datetime.now(timezone.utc).isoformat()
        with status_lock:
            cycle_progress.finish(failed=True)
            worker_state.update({
                "status": "ERROR",
                "last_cycle_finished_at": finished,
                "true_errors": 1,
                "true_error_details": [{"error": f"{type(e).__name__}: {e}"[:500]}],
                "waiting_data": 0,
                "waiting_data_details": [],
                "auxiliary_error_details": [],
                "message": f"{type(e).__name__}: {e}",
            })
        _write_status()
        print(stamp, "AUTO_ERROR", type(e).__name__, e, flush=True)
    _sleep_until_due()
