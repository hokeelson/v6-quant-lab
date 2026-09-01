from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import runtime_health_exporter as rhe
from src.worker_progress import PROGRESS_SCHEMA_VERSION, NO_PROGRESS_TIMEOUT_SECONDS


def _iso(delta_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(rhe, "MAIN_STATUS_PATH", tmp_path / "worker_status.json")
    monkeypatch.setattr(rhe, "V2_STATUS_PATH", tmp_path / "v2_status.json")
    monkeypatch.setattr(rhe, "RESEARCH_PATH", tmp_path / "research_snapshot.json")
    monkeypatch.setattr(rhe, "V2_SNAPSHOT_PATH", tmp_path / "v2_snapshot.json")
    monkeypatch.setattr(rhe, "STORAGE_PATH", tmp_path / "storage.json")
    monkeypatch.setattr(rhe, "DIRECTION_STATUS_PATH", tmp_path / "direction.json")
    monkeypatch.setattr(rhe, "DIRECTION_BACKUP_PATH", tmp_path / "direction_backup.json")


def _seed_healthy(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    _write(rhe.DIRECTION_STATUS_PATH, {
        "status": "ONLINE", "heartbeat_at": _iso(-5),
        "last_cycle_finished_at": _iso(-100), "candidates": 2, "pending": 2,
        "evaluated": 0, "shared_cache_only": True, "true_errors": 0,
    })
    _write(rhe.DIRECTION_BACKUP_PATH, {"success": True, "last_snapshot_at": _iso(-30), "pending": 2, "evaluated": 0})
    _write(rhe.MAIN_STATUS_PATH, {
        "status": "ONLINE",
        "heartbeat_at": _iso(-5),
        "last_cycle_started_at": _iso(-45),
        "last_cycle_finished_at": _iso(-10),
        "bars_processed": 3,
        "assets_checked": 20,
        "true_errors": 0,
        "risk_layer": "ONLINE",
        "data_quality": "ONLINE",
        "realtime_watchlist_sync": "ONLINE",
        "broker_order_api_calls": 0,
        "market_data_api_calls": 12,
    })
    _write(rhe.V2_STATUS_PATH, {
        "status": "ONLINE",
        "started_at": _iso(-40),
        "finished_at": _iso(-10),
        "persistent_checkpoint": True,
        "broker_order_api_calls": 0,
        "market_data_api_calls": 0,
    })
    _write(rhe.RESEARCH_PATH, {
        "contains_secrets": False,
        "scope": "PUBLIC_READ_ONLY_RESEARCH_SUMMARY",
        "generated_at": _iso(-20),
        "accounts": [{"as_of": _iso(-60)}],
    })
    _write(rhe.V2_SNAPSHOT_PATH, {
        "status": "ONLINE",
        "generated_at": _iso(-15),
        "persistent_checkpoint": True,
        "broker_order_api_calls": 0,
        "market_data_api_calls": 0,
        "research_layer_present": True,
        "tracked_research_trades": 6,
        "active_blocked_candidates": 12,
        "catchup": {"is_catching_up": False, "remaining_events_estimate": 0},
    })
    _write(rhe.STORAGE_PATH, {
        "status": "AVAILABLE",
        "persistence_status": "HEALTHY",
        "last_snapshot_success": True,
        "updated_at": _iso(-5),
    })


def test_runtime_health_is_healthy_when_workers_and_snapshots_are_fresh(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    result = rhe.build_snapshot()
    assert result["overall_status"] == "HEALTHY"
    assert result["source"] == "RAILWAY_RUNTIME_DIRECT"
    assert result["safety"]["simulation_only"] is True
    assert result["safety"]["broker_order_api_calls"] == 0
    assert result["components"]["main_v8"]["healthy"] is True
    assert result["components"]["crypto_v2"]["healthy"] is True
    assert result["components"]["crypto_v2"]["tracked_research_trades"] == 6
    assert result["components"]["crypto_v2"]["active_blocked_candidates"] == 12


def test_stale_research_degrades_observability_without_marking_workers_dead(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    research = json.loads(rhe.RESEARCH_PATH.read_text(encoding="utf-8"))
    research["generated_at"] = _iso(-(rhe.RESEARCH_MAX_AGE + 30))
    _write(rhe.RESEARCH_PATH, research)
    result = rhe.build_snapshot()
    assert result["overall_status"] == "DEGRADED"
    assert result["components"]["main_v8"]["healthy"] is True
    assert result["components"]["crypto_v2"]["healthy"] is True
    assert result["components"]["research"]["healthy"] is False


def test_stale_main_worker_heartbeat_is_error(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    main = json.loads(rhe.MAIN_STATUS_PATH.read_text(encoding="utf-8"))
    main["heartbeat_at"] = _iso(-(rhe.MAIN_HEARTBEAT_MAX_AGE + 30))
    _write(rhe.MAIN_STATUS_PATH, main)
    result = rhe.build_snapshot()
    assert result["overall_status"] == "ERROR"
    assert result["components"]["main_v8"]["healthy"] is False


def test_v2_broker_call_violation_is_error(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    v2 = json.loads(rhe.V2_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    v2["broker_order_api_calls"] = 1
    _write(rhe.V2_SNAPSHOT_PATH, v2)
    result = rhe.build_snapshot()
    assert result["overall_status"] == "ERROR"
    assert result["safety"]["broker_order_api_calls"] == 1
    assert result["components"]["crypto_v2"]["healthy"] is False


def test_missing_direction_worker_degrades_overall_health(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    rhe.DIRECTION_STATUS_PATH.unlink()
    result = rhe.build_snapshot()
    assert result["overall_status"] == "DEGRADED"
    assert result["components"]["direction_v10"]["healthy"] is False


def test_missing_direction_backup_degrades_overall_health(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    rhe.DIRECTION_BACKUP_PATH.unlink()
    result = rhe.build_snapshot()
    assert result["overall_status"] == "DEGRADED"
    assert result["components"]["direction_v10"]["backup_healthy"] is False


def _progress_main():
    return {
        "status": "RUNNING", "heartbeat_at": _iso(-2),
        "last_cycle_started_at": _iso(-1800), "last_cycle_finished_at": _iso(-1900),
        "first_cycle_complete": True, "progress_schema_version": PROGRESS_SCHEMA_VERSION,
        "last_progress_at": _iso(-10), "progress_events": 30,
        "phase": "SIMULATION", "phase_completed": 25, "phase_total": 100,
        "phase_elapsed_seconds": 500, "phase_durations_seconds": {"CALIBRATION": 900},
        "risk_layer": "ONLINE", "data_quality": "OK", "realtime_watchlist_sync": "ONLINE",
    }


def test_productive_long_cycle_is_healthy_and_exports_progress(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    _write(rhe.MAIN_STATUS_PATH, _progress_main())
    result = rhe.build_snapshot()
    assert result["overall_status"] == "HEALTHY"
    main = result["components"]["main_v8"]
    assert main["healthy"] is True
    assert main["ready"] is True
    assert main["phase_completed"] == 25
    assert main["phase_durations_seconds"] == {"CALIBRATION": 900}


def test_startup_without_completed_cycle_is_not_healthy(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    _write(rhe.MAIN_STATUS_PATH, {
        **_progress_main(), "first_cycle_complete": False,
        "last_cycle_finished_at": None, "risk_layer": "STARTING", "data_quality": "STARTING",
        "realtime_watchlist_sync": "STARTING",
    })
    result = rhe.build_snapshot()
    assert result["overall_status"] == "STARTING"
    main = result["components"]["main_v8"]
    assert main["healthy"] is False
    assert main["ready"] is False
    assert main["starting"] is True
    assert main["hard_failure"] is False


def test_legacy_startup_is_not_falsely_healthy(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    _write(rhe.MAIN_STATUS_PATH, {
        "status": "RUNNING", "heartbeat_at": _iso(-2),
        "last_cycle_started_at": _iso(-300), "last_cycle_finished_at": None,
        "risk_layer": "STARTING", "data_quality": "STARTING", "assets_checked": 0,
    })
    assert rhe.build_snapshot()["overall_status"] == "STARTING"


def test_stalled_work_is_error_even_with_fresh_heartbeat(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    _write(rhe.MAIN_STATUS_PATH, {
        **_progress_main(), "last_progress_at": _iso(-NO_PROGRESS_TIMEOUT_SECONDS - 10),
    })
    result = rhe.build_snapshot()
    assert result["overall_status"] == "ERROR"
    assert "no work progress" in result["components"]["main_v8"]["progress_problem"]


def test_errors_are_not_hidden_behind_starting_state(monkeypatch, tmp_path):
    _seed_healthy(monkeypatch, tmp_path)
    _write(rhe.MAIN_STATUS_PATH, {
        **_progress_main(), "first_cycle_complete": False, "true_errors": 1,
    })
    assert rhe.build_snapshot()["overall_status"] == "DEGRADED"
