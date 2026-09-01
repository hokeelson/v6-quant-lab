from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import worker_supervisor_v8 as supervisor
from src.worker_progress import PROGRESS_SCHEMA_VERSION, NO_PROGRESS_TIMEOUT_SECONDS


def _write(path, **payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_supervisor_accepts_fresh_running_cycle(tmp_path, monkeypatch):
    status = tmp_path / "worker_status.json"
    monkeypatch.setattr(supervisor, "STATUS_PATH", status)
    now = datetime.now(timezone.utc)
    _write(
        status,
        status="RUNNING",
        heartbeat_at=now.isoformat(),
        last_cycle_started_at=(now - timedelta(seconds=30)).isoformat(),
    )
    assert supervisor._restart_reason(time.time() - 60) is None


def test_supervisor_restarts_hung_cycle_even_with_fresh_heartbeat(tmp_path, monkeypatch):
    status = tmp_path / "worker_status.json"
    monkeypatch.setattr(supervisor, "STATUS_PATH", status)
    now = datetime.now(timezone.utc)
    _write(
        status,
        status="RUNNING",
        heartbeat_at=now.isoformat(),
        last_cycle_started_at=(now - timedelta(seconds=supervisor.MAX_CYCLE_SECONDS + 30)).isoformat(),
    )
    reason = supervisor._restart_reason(time.time() - supervisor.MAX_CYCLE_SECONDS - 60)
    assert reason is not None
    assert "cycle exceeded" in reason


def test_supervisor_restarts_stale_heartbeat(tmp_path, monkeypatch):
    status = tmp_path / "worker_status.json"
    monkeypatch.setattr(supervisor, "STATUS_PATH", status)
    now = datetime.now(timezone.utc)
    _write(
        status,
        status="ONLINE",
        heartbeat_at=(now - timedelta(seconds=supervisor.HEARTBEAT_STALE_SECONDS + 30)).isoformat(),
        last_cycle_started_at=(now - timedelta(seconds=60)).isoformat(),
    )
    reason = supervisor._restart_reason(time.time() - 600)
    assert reason is not None
    assert "heartbeat stale" in reason


def _progress_payload():
    now = datetime.now(timezone.utc)
    return {
        "pid": 123, "status": "RUNNING", "heartbeat_at": now.isoformat(),
        "last_cycle_started_at": (now - timedelta(seconds=1800)).isoformat(),
        "progress_schema_version": PROGRESS_SCHEMA_VERSION,
        "last_progress_at": (now - timedelta(seconds=20)).isoformat(),
        "progress_events": 18,
    }


def test_supervisor_allows_productive_cycle_over_15_minutes(monkeypatch):
    monkeypatch.setattr(supervisor, "_read_status", _progress_payload)
    assert supervisor._restart_reason(time.time() - 2000, expected_pid=123) is None


def test_supervisor_detects_stalled_progress_even_when_heartbeat_is_fresh(monkeypatch):
    payload = _progress_payload()
    payload["last_progress_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=NO_PROGRESS_TIMEOUT_SECONDS + 30)
    ).isoformat()
    monkeypatch.setattr(supervisor, "_read_status", lambda: payload)
    assert "no work progress" in supervisor._restart_reason(time.time() - 2000, expected_pid=123)


def test_supervisor_ignores_previous_process_status_during_startup_grace(monkeypatch):
    payload = _progress_payload()
    payload["last_cycle_started_at"] = "2025-01-01T00:00:00Z"
    monkeypatch.setattr(supervisor, "_read_status", lambda: payload)
    assert supervisor._restart_reason(time.time() - 30, expected_pid=456) is None
    reason = supervisor._restart_reason(time.time() - 300, expected_pid=456)
    assert "current process" in reason


def test_supervisor_absolute_limit_cannot_be_extended_by_progress(monkeypatch):
    payload = _progress_payload()
    payload["last_cycle_started_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=supervisor.MAX_CYCLE_SECONDS + 30)
    ).isoformat()
    monkeypatch.setattr(supervisor, "_read_status", lambda: payload)
    assert "absolute" in supervisor._restart_reason(time.time() - 4000, expected_pid=123)
