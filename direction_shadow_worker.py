from __future__ import annotations

import json
import time
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.decision_engine import HORIZON_SPECS, atr
from src.direction_forward import DirectionForwardLedger, ENGINE_VERSION
from src.direction_engine import assess_direction
from src.market_cache import MarketCache, TIMEFRAME_MAP
from src.paths import db_path
from src.simulation_db import SimulationDB
from src.symbol_strategy_health import find_symbol_strategy_health, symbol_strategy_health_snapshot

PUBLIC_PATH = Path("static") / "direction_shadow_snapshot.json"
REFRESH_SECONDS = 900
RETRY_SECONDS = 60
HEARTBEAT_SECONDS = 15
STATUS_PATH = Path(db_path("direction_shadow_worker_status.json"))


def open_runtime():
    """Never silently initialize a different, empty input DB under the app cwd."""
    simulation_path = Path(db_path("simulation_lab.sqlite3"))
    cache_path = Path(db_path("market_cache.sqlite3"))
    missing = [path.name for path in (simulation_path, cache_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError("Shared runtime input DB missing: " + ", ".join(missing))
    return (
        SimulationDB(str(simulation_path)),
        MarketCache(str(cache_path)),
        DirectionForwardLedger(db_path("direction_forward.sqlite3")),
    )


class WorkerStatus:
    def __init__(self, path=STATUS_PATH):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.state = {
            "status": "STARTING", "pid": os.getpid(), "engine_version": ENGINE_VERSION,
            "broker_order_api_calls": 0, "market_data_api_calls": 0,
            "shared_cache_only": True, "true_errors": 0,
        }

    def update(self, **updates):
        with self.lock:
            self.state.update(updates)
            self.state["heartbeat_at"] = _now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, allow_nan=False), encoding="utf-8")
            tmp.replace(self.path)

    def heartbeat(self, stop):
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                self.update()
            except Exception as exc:
                print("DIRECTION_HEARTBEAT_ERROR", type(exc).__name__, flush=True)



def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_snapshot(db: SimulationDB, cache: MarketCache, forward: DirectionForwardLedger | None = None) -> dict:
    forward = forward or DirectionForwardLedger(db_path("direction_forward.sqlite3"))
    rows = []
    errors = []
    counts = {"assets_checked": 0, "eligible_assets": 0, "models_found": 0,
              "missing_models": 0, "insufficient_cache": 0, "registered": 0}
    try:
        health_snapshot = symbol_strategy_health_snapshot(db)
    except Exception as exc:
        health_snapshot = {"symbols": [], "shadow_only": True}
        errors.append({"component": "symbol_strategy_health", "error": f"{type(exc).__name__}: {exc}"})
    for asset in db.assets():
        counts["assets_checked"] += 1
        market = str(asset.get("market") or "")
        symbol = str(asset.get("symbol") or "").upper()
        if market != "crypto" or not symbol:
            continue
        counts["eligible_assets"] += 1
        for horizon in ("short", "medium", "long"):
            try:
                model = db.model(market, symbol, horizon)
                if not model:
                    counts["missing_models"] += 1
                    continue
                counts["models_found"] += 1
                stock_tf, crypto_tf = TIMEFRAME_MAP[(market, horizon)]
                timeframe = stock_tf if market == "stock" else crypto_tf
                # Consume the main worker's cache, never make additional market API calls.
                df = cache.closed_only(cache.get(market, symbol, timeframe), market, horizon)
                if df is None or len(df) < 80:
                    counts["insufficient_cache"] += 1
                    continue
                evaluation = forward.evaluate_pair(df, market, symbol, horizon)
                a = atr(df, 14)
                px = float(df.close.iloc[-1])
                atr_pct = float(a.iloc[-1] / px) if px > 0 and a.iloc[-1] == a.iloc[-1] else 0.03
                spec = HORIZON_SPECS[horizon]
                stop = max(0.01, min(0.30, float(spec["atr_stop"]) * atr_pct))
                target = max(0.02, min(0.80, float(spec["atr_target"]) * atr_pct))
                strategy = str(model.get("strategy") or "")
                performance_health = find_symbol_strategy_health(
                    health_snapshot, market, symbol, horizon, strategy
                ) or {}
                diagnostics = model.get("diagnostics") or {}
                performance_health = {
                    **performance_health,
                    "model_stability": diagnostics.get("stability", 50.0),
                    "model_sample": diagnostics.get("sample", 0.0),
                }
                result = assess_direction(
                    df, market, strategy, stop, target,
                    performance_health=performance_health,
                )
                row = {
                    "market": market,
                    "symbol": symbol,
                    "horizon": horizon,
                    "strategy": model.get("strategy"),
                    "as_of": df.index[-1].isoformat(),
                    "close": px,
                    "stop_distance": stop,
                    "target_distance": target,
                    "engine_version": ENGINE_VERSION,
                    "forward_evaluation": evaluation,
                    **result,
                }
                registration = forward.register(row)
                counts["registered"] += int(registration.get("registered", False))
                row["forward_registration"] = registration
                rows.append(row)
            except Exception as exc:
                errors.append({"market": market, "symbol": symbol, "horizon": horizon, "error": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda r: (float(r.get("direction_confidence") or 0.0), float(r.get("ev_gap_r") or 0.0)), reverse=True)
    forward_summary = forward.summary()
    status = "ONLINE" if rows and not errors and (forward_summary["pending"] + forward_summary["evaluated"]) > 0 else "DEGRADED"
    return {
        "status": status,
        "generated_at": _now_iso(),
        "scope": "PUBLIC_READ_ONLY_DIRECTION_SHADOW",
        "decision_engine_version": "V10_ADAPTIVE_EVIDENCE_SHADOW",
        "contains_secrets": False,
        "shadow_only": True,
        "short_execution_enabled": False,
        "broker_order_api_calls": 0,
        "rows": rows,
        "forward": forward_summary,
        "shared_cache_only": True,
        "market_data_api_calls": 0,
        "summary": {
            **counts,
            "candidates": len(rows),
            "long": sum(1 for r in rows if r.get("direction") == "LONG"),
            "short": sum(1 for r in rows if r.get("direction") == "SHORT"),
            "no_trade": sum(1 for r in rows if r.get("direction") == "NO_TRADE"),
            "errors": len(errors),
            "volume_available": sum(1 for r in rows if (r.get("volume_evidence") or {}).get("status") == "AVAILABLE"),
            "forward_health_linked": sum(1 for r in rows if ((r.get("forward_stability") or {}).get("samples") or 0) > 0),
        },
        "errors": errors[:50],
    }


def write_snapshot(db, cache, forward=None):
    payload = build_snapshot(db, cache, forward)
    PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PUBLIC_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(PUBLIC_PATH)
    return payload


def run_cycle(status):
    status.update(status="RUNNING", last_cycle_started_at=_now_iso())
    try:
        db, cache, forward = open_runtime()
        snapshot = write_snapshot(db, cache, forward)
        counts = snapshot["summary"]
        status.update(
            status=snapshot["status"], last_cycle_finished_at=_now_iso(),
            candidates=counts["candidates"], registered=counts["registered"],
            eligible_assets=counts["eligible_assets"], models_found=counts["models_found"],
            missing_models=counts["missing_models"], insufficient_cache=counts["insufficient_cache"],
            pending=snapshot["forward"]["pending"], evaluated=snapshot["forward"]["evaluated"],
            true_errors=counts["errors"], true_error_details=snapshot["errors"],
            input_path_mode="SHARED_V6_DATA_DIR",
        )
        print("DIRECTION_CYCLE", snapshot["status"], json.dumps(counts), flush=True)
        return snapshot
    except Exception as exc:
        # Persist an explicit failure; do not report an empty ledger as healthy.
        status.update(status="ERROR", last_cycle_finished_at=_now_iso(),
                      true_errors=1, true_error_details=[{"error": type(exc).__name__}])
        print("DIRECTION_CYCLE_ERROR", type(exc).__name__, flush=True)
        return None


def main():
    status = WorkerStatus()
    stop = threading.Event()
    status.update()
    heartbeat = threading.Thread(target=status.heartbeat, args=(stop,), daemon=True)
    heartbeat.start()
    try:
        while True:
            snapshot = run_cycle(status)
            delay = REFRESH_SECONDS if snapshot and snapshot["status"] == "ONLINE" else RETRY_SECONDS
            stop.wait(delay)
    finally:
        stop.set()
        heartbeat.join(timeout=HEARTBEAT_SECONDS + 1)


if __name__ == "__main__":
    main()
