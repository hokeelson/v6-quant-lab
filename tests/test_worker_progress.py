from __future__ import annotations

import ast
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.status_file import atomic_write_json
from src.worker_progress import (
    CYCLE_HARD_LIMIT_SECONDS, NO_PROGRESS_TIMEOUT_SECONDS, PROGRESS_SCHEMA_VERSION,
    CycleProgress, notify_progress, public_progress, running_progress_problem,
)
from src.worker_progress_ui import render_worker_progress


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.base = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def tick(self):
        return self.seconds

    def wall(self):
        return self.base + timedelta(seconds=self.seconds)


def _running(now, cycle_age=1800, progress_age=10):
    return {
        "status": "RUNNING", "progress_schema_version": PROGRESS_SCHEMA_VERSION,
        "last_cycle_started_at": (now - timedelta(seconds=cycle_age)).isoformat(),
        "last_progress_at": (now - timedelta(seconds=progress_age)).isoformat(),
        "progress_events": 7,
    }


def test_snapshot_and_heartbeat_do_not_advance_work_progress():
    clock = Clock()
    p = CycleProgress(clock.tick, clock.wall)
    p.start()
    p.report("CALIBRATION", completed=0, total=4, unit="crypto:BTCUSDT:short")
    before = p.snapshot()
    clock.seconds = 1000
    after = p.snapshot()
    assert after["last_progress_at"] == before["last_progress_at"]
    assert after["progress_events"] == before["progress_events"]
    assert after["phase_elapsed_seconds"] == 1000
    raw = {**after, "last_cycle_started_at": clock.base.isoformat()}
    assert "no work progress" in running_progress_problem(raw, now=clock.wall())


def test_slow_units_and_history_are_bounded_and_retained():
    clock = Clock()
    p = CycleProgress(clock.tick, clock.wall)
    for cycle in range(25):
        p.start()
        for unit in range(30):
            clock.seconds += 1
            p.report("SIMULATION", unit=f"crypto:T{unit}:short", unit_seconds=unit, unit_bars=2)
        p.finish(failed=cycle == 24)
    snap = public_progress(p.snapshot())
    assert len(snap["recent_cycles"]) == 20
    assert snap["recent_cycles"][-1]["status"] == "FAILED"
    assert len(snap["last_cycle_slow_units"]) == 10
    assert snap["last_cycle_slow_units"][0]["seconds"] == 29
    p.start()
    assert p.snapshot()["slow_units"] == []
    assert len(p.snapshot()["last_cycle_slow_units"]) == 10
    snap["recent_cycles"][0]["status"] = "MODIFIED"
    assert p.snapshot()["recent_cycles"][0]["status"] == "COMPLETE"


def test_nested_progress_public_fields_strip_unexpected_keys():
    p = public_progress({"slow_units": [{"unit": "TEST", "seconds": float("nan"), "token": "secret"}],
                         "recent_cycles": [{"finished_at": "x", "duration_seconds": 12, "password": "secret"}]})
    assert p["slow_units"] == [{"unit": "TEST"}]
    assert p["recent_cycles"] == [{"finished_at": "x", "duration_seconds": 12}]


def test_phase_timing_and_completed_cycle_are_retained_separately():
    clock = Clock()
    p = CycleProgress(clock.tick, clock.wall)
    p.start()
    clock.seconds = 2
    p.report("CALIBRATION", total=4)
    clock.seconds = 12
    p.report("CALIBRATION", completed=1, total=4)
    clock.seconds = 22
    p.report("SIMULATION", total=9)
    clock.seconds = 30
    p.finish()
    result = p.snapshot()
    assert result["last_cycle_duration_seconds"] == 30
    assert result["last_cycle_phase_durations_seconds"] == {"PREPARE": 2, "CALIBRATION": 20, "SIMULATION": 8}
    clock.seconds = 90
    p.start()
    result = p.snapshot()
    assert result["phase_completed"] == 0
    assert result["cycle_elapsed_seconds"] == 0
    assert result["last_cycle_duration_seconds"] == 30
    assert result["last_cycle_phase_durations_seconds"]["CALIBRATION"] == 20


def test_failure_preserves_timing_and_finishes_the_cycle():
    clock = Clock()
    p = CycleProgress(clock.tick, clock.wall)
    p.start()
    p.report("SIMULATION")
    clock.seconds = 7
    p.finish(failed=True)
    assert p.snapshot()["phase"] == "FAILED"
    assert p.snapshot()["last_cycle_phase_durations_seconds"]["SIMULATION"] == 7


def test_productive_long_cycle_is_allowed_but_cannot_run_forever():
    now = datetime.now(timezone.utc)
    assert running_progress_problem(_running(now), now) is None
    reason = running_progress_problem(_running(now, CYCLE_HARD_LIMIT_SECONDS + 1), now)
    assert "absolute" in reason


def test_stalled_progress_is_rejected_with_a_fresh_heartbeat():
    now = datetime.now(timezone.utc)
    raw = {**_running(now, progress_age=NO_PROGRESS_TIMEOUT_SECONDS + 1), "heartbeat_at": now.isoformat()}
    assert "no work progress" in running_progress_problem(raw, now)


@pytest.mark.parametrize("value", [None, "bad", "2025-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_missing_old_or_future_progress_cannot_renew_watchdog(value):
    now = datetime.now(timezone.utc)
    raw = {**_running(now), "last_progress_at": value}
    assert "invalid progress" in running_progress_problem(raw, now)


def test_no_progress_events_is_invalid():
    now = datetime.now(timezone.utc)
    raw = {**_running(now), "progress_events": 0}
    assert "missing progress events" in running_progress_problem(raw, now)


def test_legacy_worker_retains_its_bounded_timeout():
    now = datetime.now(timezone.utc)
    raw = _running(now)
    raw.pop("progress_schema_version")
    assert "legacy cycle exceeded" in running_progress_problem(raw, now)


def test_observer_error_does_not_change_business_execution(capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("sensitive exception content")
    notify_progress(broken, "SIMULATION")
    output = capsys.readouterr().out
    assert "WORKER_PROGRESS_ERROR RuntimeError" in output
    assert "sensitive exception content" not in output


def test_public_progress_has_an_explicit_allowlist():
    raw = {"phase": "SIMULATION", "pid": 42, "token": "hidden", "error": "hidden",
           "phase_durations_seconds": {"SIMULATION": 12, "secret": 999, "CALIBRATION": float("nan")}}
    assert public_progress(raw) == {"phase": "SIMULATION", "phase_durations_seconds": {"SIMULATION": 12.0}}


def _worker_writer_namespace(tmp_path):
    # Load just the actual writer functions: importing the worker's legacy
    # executable module would start an infinite production loop and open DBs.
    source = (Path(__file__).parents[1] / "live_worker_v8.py").read_text()
    names = {"_write_status", "_report_progress"}
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
    progress = CycleProgress()
    progress.start()
    ns = {"json": json, "datetime": datetime, "timezone": timezone,
          "atomic_write_json": atomic_write_json,
          "status_lock": threading.Lock(), "status_path": tmp_path / "worker_status.json",
          "worker_state": {"status": "RUNNING"}, "cycle_progress": progress}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "live_worker_v8.py", "exec"), ns)
    return ns


def test_heartbeat_and_progress_atomic_writes_do_not_race(tmp_path):
    ns = _worker_writer_namespace(tmp_path)
    def write(i):
        if i % 2:
            ns["_write_status"]()
        else:
            ns["_report_progress"]("SIMULATION", completed=i, total=100,
                                   metrics={"assets_checked": i, "bars_processed": i})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(100)))
    payload = json.loads(ns["status_path"].read_text())
    assert payload["progress_schema_version"] == PROGRESS_SCHEMA_VERSION
    assert payload["progress_events"] == 51
    before = payload["last_progress_at"]
    ns["_write_status"]()
    assert json.loads(ns["status_path"].read_text())["last_progress_at"] == before
    assert not ns["status_path"].with_suffix(".json.tmp").exists()


def test_running_loop_resets_counts_and_passes_the_progress_callback():
    source = (Path(__file__).parents[1] / "live_worker_v8.py").read_text()
    assert '"assets_checked": 0' in source
    assert "force_recalibrate=force_recalibrate, progress=_report_progress" in source
    assert "cycle_progress.finish(failed=True)" in source


def test_shared_dashboard_displays_phase_counts_and_timings():
    captured = []
    class Panel:
        def caption(self, value): captured.append(value)
        def progress(self, value): captured.append(value)
        def expander(self, *args, **kwargs): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def dataframe(self, value, **kwargs): captured.append(value)
    render_worker_progress(Panel(), {
        "progress_schema_version": PROGRESS_SCHEMA_VERSION,
        "phase": "CALIBRATION", "phase_completed": 2, "phase_total": 4,
        "progress_unit": "crypto:BTCUSDT:short", "phase_elapsed_seconds": 12,
        "cycle_elapsed_seconds": 20, "last_cycle_duration_seconds": 904,
        "phase_durations_seconds": {"CALIBRATION": 12},
        "last_cycle_phase_durations_seconds": {"CALIBRATION": 400},
    })
    assert "校準策略模型" in captured[0]
    assert "2/4" in captured[0]
    assert 0.5 in captured
    assert captured[-1] == [{"階段": "校準策略模型", "本輪": 12.0, "上一輪": 400.0}]
