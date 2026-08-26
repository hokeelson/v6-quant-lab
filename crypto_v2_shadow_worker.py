from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.crypto_v2.shadow_db import CryptoV2ShadowDB
from src.crypto_v2.shadow_engine import CryptoV2ShadowEngine
from src.market_cache import MarketCache
from src.paths import data_dir, db_path
from src.simulation_db import SimulationDB

POLL_SECONDS = 60
STATUS_PATH = Path(data_dir()) / "crypto_v2_shadow_worker_status.json"

baseline_db = SimulationDB(db_path("simulation_lab.sqlite3"))
cache = MarketCache(db_path("market_cache.sqlite3"))
shadow_db = CryptoV2ShadowDB(db_path("crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
engine = CryptoV2ShadowEngine(baseline_db, cache, shadow_db)


def write_status(payload: dict):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_PATH)


print("Crypto V2 Shadow Worker started.", flush=True)
print("Shared cache only. Market data API calls = 0. Broker order API calls = 0.", flush=True)

while True:
    started = datetime.now(timezone.utc).isoformat()
    try:
        result = engine.cycle()
        write_status({
            "status": result.get("status", "UNKNOWN"),
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "bars_processed": int(result.get("bars_processed", 0) or 0),
            "symbols": int(result.get("symbols", 0) or 0),
            "errors": result.get("errors") or [],
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
            "message": "Crypto V2 shadow cycle completed",
        })
        print(started, "CRYPTO_V2", result.get("status"), "bars", result.get("bars_processed"), flush=True)
    except Exception as exc:
        payload = {
            "status": "ERROR",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "bars_processed": 0,
            "symbols": 0,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
            "message": "Crypto V2 shadow cycle failed",
        }
        write_status(payload)
        print(started, "CRYPTO_V2_ERROR", type(exc).__name__, exc, flush=True)
    time.sleep(POLL_SECONDS)
