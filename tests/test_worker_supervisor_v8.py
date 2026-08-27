from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import worker_supervisor_v8 as supervisor


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
