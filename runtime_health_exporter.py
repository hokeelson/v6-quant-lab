from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir

POLL_SECONDS = 5
DATA_DIR = Path(data_dir())
STATIC_DIR = Path("static")
PUBLIC_PATH = STATIC_DIR / "runtime_health.json"
PUBLIC_ALIAS_PATH = STATIC_DIR / "health.json"
MAIN_STATUS_PATH = DATA_DIR / "worker_status.json"
V2_STATUS_PATH = DATA_DIR / "crypto_v2_shadow_worker_status.json"
RESEARCH_PATH = STATIC_DIR / "research_snapshot.json"
V2_SNAPSHOT_PATH = STATIC_DIR / "crypto_v2_shadow_snapshot.json"
STORAGE_PATH = STATIC_DIR / "storage_persistence.json"

MAIN_HEARTBEAT_MAX_AGE = 60
MAIN_COMPLETED_MAX_AGE = 300
MAIN_RUNNING_MAX_AGE = 900
V2_COMPLETED_MAX_AGE = 180
V2_RUNNING_MAX_AGE = 3600
RESEARCH_MAX_AGE = 900
V2_SNAPSHOT_MAX_AGE = 300
STORAGE_EXPORT_MAX_AGE = 30


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _parse_dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _age(raw):
    dt = _parse_dt(raw)
    if dt is None:
        return None
    return max(0.0, (_now() - dt).total_seconds())


def _file_mtime_iso(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _round_age(value):
    return None if value is None else round(float(value), 1)


def _main_health(raw: dict) -> dict:
    status = str(raw.get("status") or "UNKNOWN").upper()
    heartbeat_age = _age(raw.get("heartbeat_at"))
    cycle_age = _age(raw.get("last_cycle_started_at"))
    completed_age = _age(raw.get("last_cycle_finished_at"))
    heartbeat_ok = heartbeat_age is not None and heartbeat_age <= MAIN_HEARTBEAT_MAX_AGE
    if status == "RUNNING":
        activity_ok = cycle_age is not None and cycle_age <= MAIN_RUNNING_MAX_AGE
    else:
        activity_ok = completed_age is not None and completed_age <= MAIN_COMPLETED_MAX_AGE
    healthy = heartbeat_ok and activity_ok and status not in {"ERROR", "STOPPED"}
    return {
        "healthy": healthy,
        "status": status,
        "heartbeat_at": raw.get("heartbeat_at"),
        "heartbeat_age_seconds": _round_age(heartbeat_age),
        "last_cycle_started_at": raw.get("last_cycle_started_at"),
        "last_cycle_finished_at": raw.get("last_cycle_finished_at"),
        "cycle_age_seconds": _round_age(cycle_age),
        "completed_age_seconds": _round_age(completed_age),
        "bars_processed": int(raw.get("bars_processed", 0) or 0),
        "assets_checked": int(raw.get("assets_checked", 0) or 0),
        "true_errors": int(raw.get("true_errors", 0) or 0),
        "risk_layer": str(raw.get("risk_layer") or "UNKNOWN"),
        "data_quality": str(raw.get("data_quality") or "UNKNOWN"),
        "realtime_watchlist_sync": str(raw.get("realtime_watchlist_sync") or "UNKNOWN"),
        "broker_order_api_calls": int(raw.get("broker_order_api_calls", 0) or 0),
        "market_data_api_calls": int(raw.get("market_data_api_calls", 0) or 0),
    }


def _v2_health(raw: dict, snapshot: dict) -> dict:
    status = str(raw.get("status") or snapshot.get("status") or "UNKNOWN").upper()
    started_age = _age(raw.get("started_at"))
    finished_age = _age(raw.get("finished_at"))
    if status == "RUNNING":
        activity_ok = started_age is not None and started_age <= V2_RUNNING_MAX_AGE
    else:
        activity_ok = finished_age is not None and finished_age <= V2_COMPLETED_MAX_AGE
    snapshot_age = _age(snapshot.get("generated_at"))
    snapshot_ok = snapshot_age is not None and snapshot_age <= V2_SNAPSHOT_MAX_AGE
    broker_calls = int(snapshot.get("broker_order_api_calls", raw.get("broker_order_api_calls", 0)) or 0)
    market_calls = int(snapshot.get("market_data_api_calls", raw.get("market_data_api_calls", 0)) or 0)
    checkpoint = snapshot.get("persistent_checkpoint", raw.get("persistent_checkpoint"))
    healthy = (
        activity_ok
        and snapshot_ok
        and status not in {"ERROR", "STOPPED"}
        and checkpoint is True
        and broker_calls == 0
        and market_calls == 0
    )
    catchup = snapshot.get("catchup") if isinstance(snapshot.get("catchup"), dict) else {}
    research_layer = snapshot.get("research_layer") if isinstance(snapshot.get("research_layer"), dict) else {}
    excursion = research_layer.get("trade_excursion_tracking") if isinstance(research_layer.get("trade_excursion_tracking"), dict) else {}
    counterfactual = research_layer.get("risk_block_counterfactual") if isinstance(research_layer.get("risk_block_counterfactual"), dict) else {}
    tracked_research_trades = excursion.get("tracked_closed_trades", snapshot.get("tracked_research_trades", 0))
    active_blocked_candidates = counterfactual.get("active_candidates", snapshot.get("active_blocked_candidates", 0))
    return {
        "healthy": healthy,
        "status": status,
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "started_age_seconds": _round_age(started_age),
        "finished_age_seconds": _round_age(finished_age),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_age_seconds": _round_age(snapshot_age),
        "persistent_checkpoint": checkpoint is True,
        "backlog_remaining": int(catchup.get("remaining_events_estimate", 0) or 0),
        "is_catching_up": bool(catchup.get("is_catching_up", False)),
        "research_layer_present": bool(research_layer) or bool(snapshot.get("research_layer_present", False)),
        "tracked_research_trades": int(tracked_research_trades or 0),
        "active_blocked_candidates": int(active_blocked_candidates or 0),
        "broker_order_api_calls": broker_calls,
        "market_data_api_calls": market_calls,
    }


def _research_health(raw: dict) -> dict:
    generated_age = _age(raw.get("generated_at"))
    healthy = (
        raw.get("contains_secrets") is False
        and str(raw.get("scope") or "") == "PUBLIC_READ_ONLY_RESEARCH_SUMMARY"
        and generated_age is not None
        and generated_age <= RESEARCH_MAX_AGE
    )
    account_as_of = None
    accounts = raw.get("accounts")
    if isinstance(accounts, list):
        times = [str(x.get("as_of")) for x in accounts if isinstance(x, dict) and x.get("as_of")]
        account_as_of = max(times) if times else None
    return {
        "healthy": healthy,
        "generated_at": raw.get("generated_at"),
        "age_seconds": _round_age(generated_age),
        "latest_account_as_of": account_as_of,
    }


def _storage_health(raw: dict) -> dict:
    status = str(raw.get("status") or "UNAVAILABLE").upper()
    persistence = str(raw.get("persistence_status") or "UNKNOWN").upper()
    exporter_heartbeat_at = _file_mtime_iso(STORAGE_PATH)
    exporter_age = _age(exporter_heartbeat_at)
    exporter_fresh = exporter_age is not None and exporter_age <= STORAGE_EXPORT_MAX_AGE
    return {
        "healthy": exporter_fresh and status == "AVAILABLE" and persistence not in {"FAILED", "ERROR"},
        "status": status,
        "persistence_status": persistence,
        "exporter_heartbeat_at": exporter_heartbeat_at,
        "exporter_heartbeat_age_seconds": _round_age(exporter_age),
        "last_snapshot_at": raw.get("last_snapshot_at"),
        "last_snapshot_success": raw.get("last_snapshot_success"),
        "source_updated_at": raw.get("updated_at"),
    }


def build_snapshot() -> dict:
    main_raw = _read_json(MAIN_STATUS_PATH)
    v2_raw = _read_json(V2_STATUS_PATH)
    research_raw = _read_json(RESEARCH_PATH)
    v2_snapshot = _read_json(V2_SNAPSHOT_PATH)
    storage_raw = _read_json(STORAGE_PATH)

    main = _main_health(main_raw)
    v2 = _v2_health(v2_raw, v2_snapshot)
    research = _research_health(research_raw)
    storage = _storage_health(storage_raw)

    broker_calls = int(main.get("broker_order_api_calls", 0) or 0) + int(v2.get("broker_order_api_calls", 0) or 0)
    safety_ok = broker_calls == 0 and int(v2.get("market_data_api_calls", 0) or 0) == 0
    critical_ok = bool(main.get("healthy")) and bool(v2.get("healthy")) and safety_ok
    observation_ok = bool(research.get("healthy")) and bool(storage.get("healthy"))
    if critical_ok and observation_ok:
        overall = "HEALTHY"
    elif critical_ok:
        overall = "DEGRADED"
    else:
        overall = "ERROR"

    return {
        "scope": "PUBLIC_READ_ONLY_RUNTIME_HEALTH",
        "contains_secrets": False,
        "generated_at": _now_iso(),
        "overall_status": overall,
        "components": {
            "main_v8": main,
            "crypto_v2": v2,
            "research": research,
            "storage": storage,
        },
        "safety": {
            "broker_order_api_calls": broker_calls,
            "crypto_v2_market_data_api_calls": int(v2.get("market_data_api_calls", 0) or 0),
            "simulation_only": True,
        },
        "public_paths": {
            "runtime_health": "/app/static/runtime_health.json",
            "health_alias": "/app/static/health.json",
            "streamlit_process_health": "/_stcore/health",
        },
        "source": "RAILWAY_RUNTIME_DIRECT",
    }


def _atomic_write_path(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def _atomic_write(payload: dict):
    _atomic_write_path(PUBLIC_PATH, payload)
    _atomic_write_path(PUBLIC_ALIAS_PATH, payload)


def watch():
    time.sleep(2)
    while True:
        try:
            payload = build_snapshot()
            _atomic_write(payload)
            print("RUNTIME_HEALTH", payload.get("overall_status"), flush=True)
        except Exception as exc:
            print("RUNTIME_HEALTH_EXPORT_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    watch()
