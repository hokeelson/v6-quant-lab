import signal
import sqlite3
import time
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import prepare_deploy as maintenance


def test_resume_never_signals_reused_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "process_state", lambda pid: ("S", "new"))
    monkeypatch.setattr(maintenance.os, "kill", lambda *args: calls.append(args))
    assert maintenance.resume([{"pid": 20, "started": "old"}]) == []
    assert not calls


def test_resume_workers_before_supervisors(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "process_state", lambda pid: ("T", "same"))
    monkeypatch.setattr(maintenance.os, "kill", lambda *args: calls.append(args))
    maintenance.resume([{"pid": 4, "started": "same"}, {"pid": 100, "started": "same"}])
    assert calls == [(100, signal.SIGCONT), (4, signal.SIGCONT)]


def test_expired_guard_fails_before_inspection(monkeypatch):
    def unexpected(pid):
        raise AssertionError("must not inspect after expiry")
    monkeypatch.setattr(maintenance, "process_state", unexpected)
    with pytest.raises(TimeoutError):
        maintenance.guard([{"pid": 1}], time.monotonic() - 1)


@pytest.mark.parametrize("state,started", [("R", "same"), ("T", "different")])
def test_guard_rejects_resumed_or_reused_process(monkeypatch, state, started):
    monkeypatch.setattr(maintenance, "process_state", lambda pid: (state, started))
    with pytest.raises(RuntimeError):
        maintenance.guard([{"pid": 4, "started": "same", "name": "worker"}], time.monotonic() + 10)


def test_counts_read_only_and_quotes_identifiers(tmp_path):
    path = tmp_path / "data.sqlite3"
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE "odd""name" (value INTEGER)')
    con.execute('INSERT INTO "odd""name" VALUES (1)')
    con.commit()
    con.close()
    assert maintenance.counts(path, time.monotonic() + 10) == {'odd"name': 1}
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        maintenance.counts(missing, time.monotonic() + 10)
    assert not missing.exists()


def test_digest_calls_guard_and_detects_difference(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"first")
    calls = []
    original = maintenance.digest(path, lambda: calls.append(True))
    path.write_bytes(b"second")
    assert original != maintenance.digest(path)
    assert calls


@pytest.mark.parametrize("fail_copy", [False, True])
def test_complete_preparation_or_recovery_without_signaling_real_processes(tmp_path, monkeypatch, fail_copy):
    data, runtime = tmp_path / "data", tmp_path / "runtime"
    data.mkdir()
    runtime.mkdir()
    for name in maintenance.DATABASES:
        con = sqlite3.connect(runtime / name)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE records(value INTEGER)")
        con.execute("INSERT INTO records VALUES (42)")
        if name == "direction_forward.sqlite3":
            con.execute("CREATE TABLE direction_predictions(status TEXT)")
            con.execute("INSERT INTO direction_predictions VALUES ('PENDING')")
        con.commit()
        con.close()

    real_path = Path
    def mapped_path(value):
        return {"/data": data, "/tmp/v6-data-runtime": runtime}.get(str(value), real_path(value))
    monkeypatch.setattr(maintenance, "Path", mapped_path)
    mkdtemp = maintenance.tempfile.mkdtemp
    monkeypatch.setattr(maintenance.tempfile, "mkdtemp",
                        lambda **kw: mkdtemp(prefix=kw["prefix"], dir=tmp_path))
    items = [{"pid": 10001, "started": "same", "name": "mock-supervisor"},
             {"pid": 10002, "started": "same", "name": "mock-worker"}]
    monkeypatch.setattr(maintenance, "discover", lambda: items)
    monkeypatch.setattr(maintenance, "process_state", lambda pid: ("T", "same"))
    signals = []
    monkeypatch.setattr(maintenance.os, "kill", lambda *args: signals.append(args))
    monkeypatch.setattr(maintenance.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: None))
    module = real_path(__file__).parents[1] / "storage_rescue.py"
    if fail_copy:
        def fail(fd):
            raise OSError("injected persistence failure")
        monkeypatch.setattr(maintenance.os, "fsync", fail)
        with pytest.raises(OSError):
            maintenance.prepare(module)
        assert signals[-2:] == [(10002, signal.SIGCONT), (10001, signal.SIGCONT)]
        assert not list(data.glob("*-receipt.json"))
    else:
        maintenance.prepare(module)
        receipts = list(data.glob("*-receipt.json"))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text())
        assert set(receipt["databases"]) == set(maintenance.DATABASES)
        for name in maintenance.DATABASES:
            assert receipt["databases"][name]["counts"]["records"] == 1
            assert (data / "v6-snapshots" / "current" / name).is_file()
        # Successful readiness intentionally remains paused until deployment or guardian.
        assert signals == [(10001, signal.SIGSTOP), (10002, signal.SIGSTOP)]
