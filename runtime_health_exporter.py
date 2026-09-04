from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir
from src.worker_progress import public_progress, running_progress_problem

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
DIRECTION_STATUS_PATH = DATA_DIR / "direction_shadow_worker_status.json"
DIRECTION_BACKUP_PATH = DATA_DIR / "direction_forward_backup_status.json"
DIRECTION_MAX_CYCLE_AGE = 1800
DIRECTION_MAX_COMPLETED_AGE = 1800
DIRECTION_BACKUP_MAX_AGE = 1800

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


def _safe_error_text(value) -> str:
    text = str(value or "")[:500]
    text = re.sub(r"(?i)(bearer)\s+[^\s]+", r"\1 <redacted>", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    return text


def _public_error_samples(raw: dict, limit=8) -> list[dict]:
    rows = raw.get("true_error_details")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            out.append({"error": _safe_error_text(row)})
            continue
        item = {}
        for key in ("market", "symbol", "horizon"):
            if row.get(key) is not None:
                item[key] = str(row.get(key))[:80]
        if row.get("error") is not None:
            item["error"] = _safe_error_text(row.get("error"))
        if item:
            out.append(item)
    return out


def _main_health(raw: dict) -> dict:
    status = str(raw.get("status") or "UNKNOWN").upper()
    heartbeat_age = _age(raw.get("heartbeat_at"))
    cycle_age = _age(raw.get("last_cycle_started_at"))
    completed_age = _age(raw.get("last_cycle_finished_at"))
    heartbeat_ok = heartbeat_age is not None and heartbeat_age <= MAIN_HEARTBEAT_MAX_AGE
    progress_problem = None
    if status == "RUNNING":
        progress_problem = running_progress_problem(raw, now=_now())
        activity_ok = progress_problem is None
    elif status == "STARTING":
        started_age = _age(raw.get("worker_started_at"))
        activity_ok = started_age is not None and started_age <= MAIN_RUNNING_MAX_AGE
    else:
        activity_ok = completed_age is not None and completed_age <= MAIN_COMPLETED_MAX_AGE

    true_errors = int(raw.get("true_errors", 0) or 0)
    risk_layer = str(raw.get("risk_layer") or "UNKNOWN").upper()
    data_quality = str(raw.get("data_quality") or "UNKNOWN").upper()
    realtime_sync = str(raw.get("realtime_watchlist_sync") or "UNKNOWN").upper()

    operational = heartbeat_ok and activity_ok and status in {"STARTING", "RUNNING", "ONLINE", "DEGRADED"}
    degraded_reasons = []
    if status == "DEGRADED":
        degraded_reasons.append("worker_status_degraded")
    if true_errors > 0:
        degraded_reasons.append(f"true_errors:{true_errors}")
    if risk_layer == "ERROR":
        degraded_reasons.append("risk_layer_error")
    if data_quality == "ERROR":
        degraded_reasons.append("data_quality_error")
    if realtime_sync == "ERROR":
        degraded_reasons.append("realtime_watchlist_sync_error")

    degraded = operational and bool(degraded_reasons)
    completed_once = (raw.get("first_cycle_complete") is True if "first_cycle_complete" in raw
                      else completed_age is not None)
    ready = (completed_once and risk_layer == "ONLINE" and realtime_sync == "ONLINE"
             and data_quality not in {"UNKNOWN", "STARTING", "ERROR"})
    starting = operational and not ready and not degraded
    healthy = operational and ready and not degraded
    hard_failure = not operational

    return {
        "healthy": healthy,
        "ready": ready and operational and not degraded,
        "starting": starting,
        "degraded": degraded,
        "hard_failure": hard_failure,
        "degraded_reasons": degraded_reasons,
        "progress_problem": progress_problem,
        "status": status,
        "heartbeat_at": raw.get("heartbeat_at"),
        "heartbeat_age_seconds": _round_age(heartbeat_age),
        "last_cycle_started_at": raw.get("last_cycle_started_at"),
        "last_cycle_finished_at": raw.get("last_cycle_finished_at"),
        "cycle_age_seconds": _round_age(cycle_age),
        "completed_age_seconds": _round_age(completed_age),
        "bars_processed": int(raw.get("bars_processed", 0) or 0),
        "assets_checked": int(raw.get("assets_checked", 0) or 0),
        "true_errors": true_errors,
        "error_samples": _public_error_samples(raw),
        "risk_layer": risk_layer,
        "data_quality": data_quality,
        "realtime_watchlist_sync": realtime_sync,
        "broker_order_api_calls": int(raw.get("broker_order_api_calls", 0) or 0),
        "market_data_api_calls": int(raw.get("market_data_api_calls", 0) or 0),
        **public_progress(raw),
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


def _direction_health(raw: dict, backup: dict) -> dict:
    status = str(raw.get("status") or "UNKNOWN").upper()
    heartbeat_age = _age(raw.get("heartbeat_at"))
    activity_age = _age(raw.get("last_cycle_started_at") if status == "RUNNING"
                        else raw.get("last_cycle_finished_at"))
    limit = DIRECTION_MAX_CYCLE_AGE if status == "RUNNING" else DIRECTION_MAX_COMPLETED_AGE
    pending = int(raw.get("pending", 0) or 0)
    evaluated = int(raw.get("evaluated", 0) or 0)
    candidates = int(raw.get("candidates", 0) or 0)
    errors = int(raw.get("true_errors", 0) or 0)
    broker_calls = int(raw.get("broker_order_api_calls", 0) or 0)
    market_calls = int(raw.get("market_data_api_calls", 0) or 0)
    reasons = []
    if heartbeat_age is None or heartbeat_age > MAIN_HEARTBEAT_MAX_AGE:
        reasons.append("missing_or_stale_heartbeat")
    if activity_age is None or activity_age > limit:
        reasons.append("missing_or_stale_cycle")
    if status not in {"ONLINE", "RUNNING"}:
        reasons.append("worker_status:" + status)
    if candidates <= 0:
        reasons.append("no_candidates")
    if pending + evaluated <= 0:
        reasons.append("empty_forward_ledger")
    if errors:
        reasons.append("worker_errors")
    if broker_calls or market_calls or raw.get("shared_cache_only") is not True:
        reasons.append("safety_or_cache_only_violation")
    backup_age = _age(backup.get("last_snapshot_at"))
    backup_ok = (backup.get("success") is True and backup_age is not None
                 and backup_age <= DIRECTION_BACKUP_MAX_AGE)
    backup_pending = int(backup.get("pending", 0) or 0)
    backup_evaluated = int(backup.get("evaluated", 0) or 0)
    backup_covers_ledger = pending + evaluated > 0 and backup_pending + backup_evaluated >= pending + evaluated
    if not backup_ok:
        reasons.append("missing_failed_or_stale_direction_backup")
    elif not backup_covers_ledger:
        reasons.append("direction_backup_behind_ledger")
    return {
        "healthy": not reasons, "status": status, "degraded_reasons": reasons,
        "heartbeat_at": raw.get("heartbeat_at"), "heartbeat_age_seconds": _round_age(heartbeat_age),
        "last_cycle_finished_at": raw.get("last_cycle_finished_at"),
        "candidates": candidates, "pending": pending, "evaluated": evaluated,
        "registered_last_cycle": int(raw.get("registered", 0) or 0),
        "eligible_assets": int(raw.get("eligible_assets", 0) or 0),
        "models_found": int(raw.get("models_found", 0) or 0),
        "missing_models": int(raw.get("missing_models", 0) or 0),
        "insufficient_cache": int(raw.get("insufficient_cache", 0) or 0),
        "true_errors": errors, "error_samples": _public_error_samples(raw),
        "input_path_mode": raw.get("input_path_mode"),
        "backup_healthy": backup_ok and backup_covers_ledger, "backup_at": backup.get("last_snapshot_at"),
        "backup_pending": backup_pending, "backup_evaluated": backup_evaluated,
        "backup_age_seconds": _round_age(backup_age),
        "broker_order_api_calls": broker_calls, "market_data_api_calls": market_calls,
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
    single_crypto = os.getenv("V6_SINGLE_CRYPTO_ACCOUNT","0").strip().lower() in ("1","true","yes","on")
    v2_default = "0" if single_crypto else "1"
    v2_enabled = os.getenv("V6_ENABLE_CRYPTO_V2_SHADOW",v2_default).strip().lower() in ("1","true","yes","on")
    if v2_enabled:
        v2 = _v2_health(v2_raw, v2_snapshot)
    else:
        v2 = {
            "healthy": True,
            "status": "DISABLED",
            "broker_order_api_calls": 0,
            "market_data_api_calls": 0,
            "research_layer_present": False,
            "tracked_research_trades": 0,
            "active_blocked_candidates": 0,
        }
    research = _research_health(research_raw)
    storage = _storage_health(storage_raw)
    direction = _direction_health(_read_json(DIRECTION_STATUS_PATH), _read_json(DIRECTION_BACKUP_PATH))

    broker_calls = int(main.get("broker_order_api_calls", 0) or 0) + int(v2.get("broker_order_api_calls", 0) or 0) + direction["broker_order_api_calls"]
    safety_ok = broker_calls == 0 and int(v2.get("market_data_api_calls", 0) or 0) == 0 and direction["market_data_api_calls"] == 0
    hard_failure = bool(main.get("hard_failure")) or (v2_enabled and not bool(v2.get("healthy"))) or not safety_ok
    degraded = bool(main.get("degraded")) or not bool(research.get("healthy")) or not bool(storage.get("healthy")) or not direction["healthy"]

    if hard_failure:
        overall = "ERROR"
    elif degraded:
        overall = "DEGRADED"
    elif main.get("starting"):
        overall = "STARTING"
    else:
        overall = "HEALTHY"

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
            "direction_v10": direction,
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
