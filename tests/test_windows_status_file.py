from __future__ import annotations

import json
from pathlib import Path

import src.status_file as status_file


def test_atomic_write_json_retries_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "worker_status.json"
    real_replace = status_file.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access denied")
        return real_replace(src, dst)

    monkeypatch.setattr(status_file.os, "replace", flaky_replace)
    status_file.atomic_write_json(target, {"pid": 123, "status": "ONLINE"}, retries=4, retry_delay=0)

    assert calls["n"] == 3
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "ONLINE"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_json_uses_unique_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "worker_status.json"
    seen = []
    real_replace = status_file.os.replace

    def capture_replace(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(status_file.os, "replace", capture_replace)
    status_file.atomic_write_json(target, {"n": 1}, retry_delay=0)
    status_file.atomic_write_json(target, {"n": 2}, retry_delay=0)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 2}


def test_live_worker_status_failures_are_nonfatal_source_guard():
    source = Path("live_worker_v8.py").read_text(encoding="utf-8")
    assert "atomic_write_json(status_path, payload)" in source
    assert "WORKER_STATUS_WRITE_ERROR" in source
    assert "return False" in source
