from __future__ import annotations

import json
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from src.crypto_v2.persistence import checkpoint_shadow_db
from src.crypto_v2.research import ResearchCryptoV2ShadowDB
from src.crypto_v2.research_engine import ResearchCryptoV2ShadowEngine
from src.market_cache import MarketCache
from src.paths import data_dir, db_path
from src.simulation_db import SimulationDB

POLL_SECONDS = 60
CATCHUP_POLL_SECONDS = 5
STATUS_PATH = Path(data_dir()) / "crypto_v2_shadow_worker_status.json"
PUBLIC_SNAPSHOT_PATH = Path("static") / "crypto_v2_shadow_snapshot.json"

baseline_db = SimulationDB(db_path("simulation_lab.sqlite3"))
cache = MarketCache(db_path("market_cache.sqlite3"))
shadow_db = ResearchCryptoV2ShadowDB(db_path("crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
engine = ResearchCryptoV2ShadowEngine(baseline_db, cache, shadow_db)


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def write_status(payload: dict):
    write_json(STATUS_PATH, payload)


def checkpoint() -> bool:
    return bool(checkpoint_shadow_db(shadow_db.path))


def shutdown_handler(signum, _frame):
    ok = checkpoint()
    finished = datetime.now(timezone.utc).isoformat()
    try:
        write_status({
            "status": "STOPPING",
            "finished_at": finished,
            "persistent_checkpoint": ok,
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
            "message": f"Crypto V2 worker stopping on signal {signum}",
        })
    except Exception:
        pass
    print(finished, "CRYPTO_V2_STOPPING", "checkpoint", ok, flush=True)
    raise SystemExit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

print("Crypto V2 Shadow Worker started.", flush=True)
print("Shared cache only. Market data API calls = 0. Broker order API calls = 0.", flush=True)
print("Research overlay = observation only; it cannot alter V2 routing, sizing, or execution.", flush=True)

while True:
    started = datetime.now(timezone.utc).isoformat()
    # Publish RUNNING before the potentially long catch-up cycle. The supervisor
    # must distinguish a healthy long-running cycle from a dead worker.
    write_status({
        "status": "RUNNING",
        "started_at": started,
        "finished_at": None,
        "bars_processed": 0,
        "symbols": 0,
        "errors": [],
        "persistent_checkpoint": None,
        "market_data_api_calls": 0,
        "broker_order_api_calls": 0,
        "message": "Crypto V2 shadow cycle running",
    })
    sleep_seconds = POLL_SECONDS
    try:
        result = engine.cycle()
        checkpoint_ok = checkpoint()
        public_result = dict(result)
        public_result["contains_secrets"] = False
        public_result["scope"] = "PUBLIC_READ_ONLY_CRYPTO_V2_SHADOW"
        public_result["persistent_checkpoint"] = checkpoint_ok
        write_json(PUBLIC_SNAPSHOT_PATH, public_result)
        catchup = result.get("catchup") or {}
        catching_up = bool(catchup.get("is_catching_up"))
        if catching_up:
            sleep_seconds = CATCHUP_POLL_SECONDS
        write_status({
            "status": result.get("status", "UNKNOWN"),
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "bars_processed": int(result.get("bars_processed", 0) or 0),
            "symbols": int(result.get("symbols", 0) or 0),
            "errors": result.get("errors") or [],
            "catchup": catchup,
            "persistent_checkpoint": checkpoint_ok,
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
            "message": "Crypto V2 shadow cycle completed",
        })
        print(
            started,
            "CRYPTO_V2",
            result.get("status"),
            "bars",
            result.get("bars_processed"),
            "remaining",
            catchup.get("remaining_events_estimate"),
            "checkpoint",
            checkpoint_ok,
            flush=True,
        )
    except Exception as exc:
        checkpoint_ok = checkpoint()
        payload = {
            "status": "ERROR",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "bars_processed": 0,
            "symbols": 0,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "persistent_checkpoint": checkpoint_ok,
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
            "message": "Crypto V2 shadow cycle failed",
        }
        write_status(payload)
        write_json(PUBLIC_SNAPSHOT_PATH, {
            "status": "ERROR",
            "scope": "PUBLIC_READ_ONLY_CRYPTO_V2_SHADOW",
            "contains_secrets": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "errors": payload["errors"],
            "persistent_checkpoint": checkpoint_ok,
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
        })
        print(
            started,
            "CRYPTO_V2_ERROR",
            type(exc).__name__,
            exc,
            "checkpoint",
            checkpoint_ok,
            flush=True,
        )
    time.sleep(sleep_seconds)
