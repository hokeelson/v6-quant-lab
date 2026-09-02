import errno
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import storage_rescue as rescue


def database(path, value=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE records(value INTEGER)")
        con.execute("INSERT INTO records VALUES (?)", (value,))
        con.commit()
    return path


def values(path):
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT value FROM records").fetchall()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    data = tmp_path / "data"
    runtime = tmp_path / "runtime"
    current = data / "v6-snapshots" / "current"
    for path in (runtime, current):
        path.mkdir(parents=True, exist_ok=True)
    for name, path in {
        "PERSIST_DIR": data, "RUNTIME_DIR": runtime,
        "SNAPSHOT_DIR": current.parent, "CURRENT_DIR": current,
        "ARCHIVE_DIR": current.parent / "archive",
        "STATUS_PATH": runtime / "status.json",
    }.items():
        monkeypatch.setattr(rescue, name, path)
    monkeypatch.setattr(rescue, "KEEP_ARCHIVES", 0)
    monkeypatch.setattr(rescue, "CRITICAL_DBS", {"simulation_lab.sqlite3"})
    return runtime, current


def test_backup_pins_wal_snapshot_during_concurrent_commit(tmp_path):
    src, dst = tmp_path / "live.sqlite3", tmp_path / "saved.sqlite3"
    with closing(sqlite3.connect(src)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE payloads(value BLOB)")
        writer.executemany("INSERT INTO payloads VALUES (?)", [(b"a" * 4096,)] * 1024)
        writer.commit()
        updates = []

        def progress(phase, **details):
            if phase == "SQLITE_BACKUP" and not updates:
                assert details["pages_done"] < details["pages_total"]
                writer.execute("INSERT INTO payloads VALUES (?)", (b"late",))
                writer.commit()
                updates.append(True)

        rescue.sqlite_backup(src, dst, on_progress=progress)
        assert updates
        assert writer.execute("SELECT COUNT(*) FROM payloads").fetchone()[0] == 1025
    with closing(sqlite3.connect(dst)) as con:
        assert con.execute("SELECT COUNT(*) FROM payloads").fetchone()[0] == 1024
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_missing_source_not_created(tmp_path):
    src = tmp_path / "absent.sqlite3"
    dst = database(tmp_path / "saved.sqlite3", 9)
    with pytest.raises(FileNotFoundError):
        rescue.sqlite_backup(src, dst)
    assert not src.exists()
    assert values(dst) == [(9,)]


def test_timeout_preserves_old_destination_and_cleans_stage(tmp_path):
    src = database(tmp_path / "live.sqlite3")
    dst = database(tmp_path / "saved.sqlite3", 9)
    with pytest.raises(TimeoutError):
        rescue.sqlite_backup(src, dst, max_seconds=0)
    assert values(dst) == [(9,)]
    assert not list(tmp_path.glob("saved.sqlite3-*.tmp*"))


def test_default_deadline_is_finite(tmp_path, monkeypatch):
    src = database(tmp_path / "live.sqlite3")
    monkeypatch.setattr(rescue, "BACKUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError):
        rescue.sqlite_backup(src, tmp_path / "saved.sqlite3")


def test_observer_failure_preserves_destination(tmp_path):
    src = database(tmp_path / "live.sqlite3")
    dst = database(tmp_path / "saved.sqlite3", 9)

    def fail(*args, **kwargs):
        raise OSError("status unavailable")

    with pytest.raises(OSError):
        rescue.sqlite_backup(src, dst, on_progress=fail)
    assert values(dst) == [(9,)]
    assert not list(tmp_path.glob("saved.sqlite3-*.tmp*"))


def test_capacity_failure_preserves_current(configured, monkeypatch):
    runtime, current = configured
    src = database(runtime / "simulation_lab.sqlite3")
    dst = database(current / src.name, 9)
    monkeypatch.setattr(rescue.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    with pytest.raises(OSError) as error:
        rescue.persist_one(src, "test")
    assert error.value.errno == errno.ENOSPC
    assert values(dst) == [(9,)]
    assert not dst.with_suffix(".sqlite3.new").exists()


def test_copy_failure_removes_partial_not_current(configured, monkeypatch):
    runtime, current = configured
    src = database(runtime / "simulation_lab.sqlite3")
    dst = database(current / src.name, 9)

    def fail(fd):
        raise OSError(errno.ENOSPC, "injected full disk")

    monkeypatch.setattr(rescue.os, "fsync", fail)
    with pytest.raises(OSError):
        rescue.persist_one(src, "test")
    assert values(dst) == [(9,)]
    assert not (current / (src.name + ".new")).exists()


def test_copy_deadline_preserves_current(configured, monkeypatch):
    runtime, current = configured
    src = database(runtime / "simulation_lab.sqlite3")
    dst = database(current / src.name, 9)
    monkeypatch.setattr(rescue, "COPY_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError):
        rescue.persist_one(src, "test")
    assert values(dst) == [(9,)]
    assert not (current / (src.name + ".new")).exists()


def test_success_replaces_current_and_reports_phases(configured):
    runtime, current = configured
    src = database(runtime / "simulation_lab.sqlite3", 123)
    dst = database(current / src.name, 9)
    phases = []
    rescue.persist_one(src, "test", on_progress=lambda phase, **kw: phases.append(phase))
    assert values(dst) == [(123,)]
    assert {"SQLITE_BACKUP", "VERIFY", "PERSIST_COPY"} <= set(phases)


def test_round_reports_inflight_without_relabeling_old_result(configured, monkeypatch):
    runtime, current = configured
    database(runtime / "simulation_lab.sqlite3")
    rescue.write_status(last_snapshot_at="OLD", last_snapshot_failed=[{"error": "old"}])
    observed = []
    real = rescue.persist_one

    def capture(src, stamp, on_progress=None):
        observed.append(json.loads(rescue.STATUS_PATH.read_text()))
        return real(src, stamp, on_progress=on_progress)

    monkeypatch.setattr(rescue, "persist_one", capture)
    rescue.snapshot_all(include_direction=False)
    assert observed[0]["last_snapshot_at"] == "OLD"
    assert observed[0]["current_snapshot_phase"] == "PREPARE"
    final = json.loads(rescue.STATUS_PATH.read_text())
    assert final["current_snapshot_phase"] == "IDLE"
    assert final["last_snapshot_failed"] == []
    assert final["persistence_status"] == "OK"


def test_missing_critical_database_not_reported_success(configured):
    rescue.snapshot_all(include_direction=False)
    final = json.loads(rescue.STATUS_PATH.read_text())
    assert final["persistence_status"] == "ERROR"
    assert final["last_snapshot_failed"][0]["db"] == "simulation_lab.sqlite3"


def test_public_export_includes_only_allowed_progress_fields():
    from storage_status_exporter import _safe_storage_status
    exported = _safe_storage_status({"current_snapshot_phase": "VERIFY", "secret": "no"})
    assert exported["current_snapshot_phase"] == "VERIFY"
    assert "secret" not in exported
