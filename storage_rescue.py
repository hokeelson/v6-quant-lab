from __future__ import annotations

import json
import errno
import os
import shutil
import sqlite3
import sys
import time
import threading
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PERSIST_DIR = Path(os.getenv("V6_PERSISTENT_DATA_DIR", "/data"))
RUNTIME_DIR = Path(os.getenv("V6_RUNTIME_DATA_DIR", "/tmp/v6-data-runtime"))
SNAPSHOT_DIR = PERSIST_DIR / "v6-snapshots"
CURRENT_DIR = SNAPSHOT_DIR / "current"
ARCHIVE_DIR = SNAPSHOT_DIR / "archive"
STATUS_PATH = RUNTIME_DIR / "storage_persistence_status.json"
INTERVAL = max(60, int(os.getenv("V6_SNAPSHOT_INTERVAL_SECONDS", "60")))
# Rescue mode keeps only the latest current snapshot. Historical archives are
# disabled because the Railway persistent volume is space constrained.
KEEP_ARCHIVES = max(0, int(os.getenv("V6_SNAPSHOT_KEEP", "0")))
BACKUP_TIMEOUT_SECONDS = 90
COPY_TIMEOUT_SECONDS = 90
PERSISTENCE_RESERVE_BYTES = 64 * 1024 * 1024

# Only persist state that cannot be cheaply rebuilt from market APIs/cache.
# market_cache/realtime quote caches are intentionally excluded in rescue mode.
DEFAULT_CRITICAL_DBS = {
    "simulation_lab.sqlite3",
    "direction_forward.sqlite3",
    "forward_validation.sqlite3",
    "model_governance.sqlite3",
    "trial_ledger.sqlite3",
    "data_quality.sqlite3",
    "realtime_execution.sqlite3",
}
CRITICAL_DBS = {
    x.strip() for x in os.getenv("V6_SNAPSHOT_DBS", ",".join(sorted(DEFAULT_CRITICAL_DBS))).split(",") if x.strip()
}
# Direction evidence is irreplaceable, even when an old environment override is present.
CRITICAL_DBS.add("direction_forward.sqlite3")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(**updates):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {}
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    payload.update(updates)
    payload["updated_at"] = now_iso()
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def disk_usage_payload():
    try:
        usage = shutil.disk_usage(PERSIST_DIR)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except Exception:
        return {}


def quick_check(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        row = con.execute("PRAGMA quick_check").fetchone()
        con.close()
        return bool(row and str(row[0]).lower() == "ok")
    except Exception:
        return False


def copy_sqlite_family(src: Path, dst_dir: Path) -> Path | None:
    if not src.exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            try:
                shutil.copy2(side, Path(str(dst) + suffix))
            except Exception:
                pass
    return dst


def candidate_mtime(path: Path) -> float:
    vals = []
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            vals.append(p.stat().st_mtime)
        except Exception:
            pass
    return max(vals) if vals else 0.0


def purge_old_snapshot_copies():
    """Delete duplicate/temporary snapshot files only.

    Never touches original SQLite files directly under /data and never deletes a
    current critical *.sqlite3 snapshot. Historical archive copies are expendable
    in rescue mode and are removed first to recover capacity.
    """
    try:
        if ARCHIVE_DIR.exists():
            shutil.rmtree(ARCHIVE_DIR, ignore_errors=True)
    except Exception:
        pass

    try:
        if CURRENT_DIR.exists():
            for p in CURRENT_DIR.iterdir():
                if not p.is_file():
                    continue
                name = p.name
                # Stale temporary copies are never valid restore points.
                if name.endswith(".new") or name.endswith(".tmp"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    continue
                # Old policy may have left rebuildable DB snapshots in current.
                if name.endswith(".sqlite3") and name not in CRITICAL_DBS:
                    try:
                        p.unlink()
                    except Exception:
                        pass
    except Exception:
        pass


def bootstrap_runtime():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Free duplicate archive space before bootstrap. Current critical snapshots are
    # intentionally preserved because they may be newer than the root /data copy.
    purge_old_snapshot_copies()
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    names = set()
    for base in (PERSIST_DIR, CURRENT_DIR):
        try:
            names.update(p.name for p in base.glob("*.sqlite3") if p.is_file() and p.name in CRITICAL_DBS)
        except Exception:
            pass

    restored, warnings = [], []
    for name in sorted(names):
        candidates = []
        for label, source_dir in (("snapshot", CURRENT_DIR), ("original", PERSIST_DIR)):
            src = source_dir / name
            if not src.exists():
                continue
            test_dir = RUNTIME_DIR / ".bootstrap" / f"{label}-{name}"
            try:
                shutil.rmtree(test_dir, ignore_errors=True)
                copied = copy_sqlite_family(src, test_dir)
                if copied and quick_check(copied):
                    candidates.append((candidate_mtime(src), label, copied))
                else:
                    warnings.append(f"{name}:{label}:quick_check_failed")
            except Exception as exc:
                warnings.append(f"{name}:{label}:{type(exc).__name__}:{exc}")

        if not candidates:
            warnings.append(f"{name}:no_healthy_source")
            continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, label, picked = candidates[0]
        target = RUNTIME_DIR / name
        try:
            shutil.copy2(picked, target)
            for suffix in ("-wal", "-shm"):
                p = Path(str(picked) + suffix)
                if p.exists():
                    shutil.copy2(p, Path(str(target) + suffix))
            restored.append({"db": name, "source": label})
        except Exception as exc:
            warnings.append(f"{name}:restore:{type(exc).__name__}:{exc}")

    shutil.rmtree(RUNTIME_DIR / ".bootstrap", ignore_errors=True)
    write_status(
        mode="RESCUE_RUNTIME",
        bootstrap_at=now_iso(),
        restored=restored,
        bootstrap_warnings=warnings,
        snapshot_interval_seconds=INTERVAL,
        snapshot_db_count=len(CRITICAL_DBS),
        snapshot_keep_archives=KEEP_ARCHIVES,
        persistent_disk=disk_usage_payload(),
    )


def sqlite_backup(src: Path, dst: Path, max_seconds=None, on_progress=None):
    """Pin a committed read snapshot so concurrent writes cannot restart copying.

    All outputs are staged; any failure leaves the previous destination intact.
    Deadlines are cooperative (SQLite callbacks and bounded busy waits).
    """
    if not src.is_file():
        raise FileNotFoundError(src)
    budget = BACKUP_TIMEOUT_SECONDS if max_seconds is None else max_seconds
    deadline = time.monotonic() + budget
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=dst.name + "-", suffix=".tmp", dir=dst.parent)
    os.close(fd)
    tmp = Path(filename)

    def check_deadline():
        if time.monotonic() >= deadline:
            raise TimeoutError("SQLite snapshot exceeded time budget")

    def progress(status, remaining, total):
        check_deadline()
        if on_progress:
            on_progress("SQLITE_BACKUP", pages_done=total - remaining, pages_total=total)

    try:
        with closing(sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as src_con, \
                closing(sqlite3.connect(str(tmp), timeout=5)) as dst_con:
            interrupted = lambda: int(time.monotonic() >= deadline)
            src_con.set_progress_handler(interrupted, 10000)
            dst_con.set_progress_handler(interrupted, 10000)
            check_deadline()
            src_con.execute("BEGIN")
            src_con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            src_con.backup(dst_con, pages=256, sleep=0.02, progress=progress)
            src_con.rollback()  # Release the WAL reader before integrity checking.
            if on_progress:
                on_progress("VERIFY")
            if dst_con.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("snapshot quick_check failed")
            check_deadline()
        tmp.replace(dst)
    finally:
        # Only this invocation's unique staging files, never live WAL/SHM files.
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)


def cleanup_snapshot_storage():
    """Free only duplicate snapshot copies; never delete original /data DBs."""
    purge_old_snapshot_copies()
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    if KEEP_ARCHIVES > 0:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def persist_one(src: Path, stamp: str, on_progress=None):
    local_stage = RUNTIME_DIR / ".snapshot-stage" / src.name
    local_stage.parent.mkdir(parents=True, exist_ok=True)
    sqlite_backup(src, local_stage, max_seconds=20 if src.name == "direction_forward.sqlite3" else None,
                  on_progress=on_progress)

    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    # Dedicated direction snapshots must not share the legacy cleanup temp namespace.
    temp_dir = SNAPSHOT_DIR / ".direction-stage" if src.name == "direction_forward.sqlite3" else CURRENT_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)
    persistent_tmp = temp_dir / (src.name + ".new")
    persistent_tmp.unlink(missing_ok=True)
    persistent_final = CURRENT_DIR / src.name
    size = local_stage.stat().st_size
    if shutil.disk_usage(PERSIST_DIR).free < size + PERSISTENCE_RESERVE_BYTES:
        raise OSError(errno.ENOSPC, "insufficient space for staged snapshot plus reserve")
    deadline = time.monotonic() + COPY_TIMEOUT_SECONDS
    try:
        copied = 0
        with local_stage.open("rb") as reader, persistent_tmp.open("xb") as writer:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("persistent snapshot copy exceeded time budget")
                block = reader.read(4 * 1024 * 1024)
                if not block:
                    break
                writer.write(block)
                copied += len(block)
                if on_progress:
                    on_progress("PERSIST_COPY", bytes_done=copied, bytes_total=size)
            writer.flush()
            os.fsync(writer.fileno())
        if time.monotonic() >= deadline:
            raise TimeoutError("persistent snapshot copy exceeded time budget")
        if copied != size or persistent_tmp.stat().st_size != size:
            raise OSError("persistent snapshot size mismatch")
        persistent_tmp.replace(persistent_final)
    finally:
        persistent_tmp.unlink(missing_ok=True)

    if KEEP_ARCHIVES > 0:
        try:
            free = shutil.disk_usage(PERSIST_DIR).free
            need = max(local_stage.stat().st_size * 2, 8 * 1024 * 1024)
            if free > need:
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_stage, ARCHIVE_DIR / f"{stamp}__{src.name}")
        except Exception:
            pass
    if src.name == "direction_forward.sqlite3":
        with sqlite3.connect(f"file:{persistent_final}?mode=ro", uri=True, timeout=10) as con:
            counts = dict(con.execute("SELECT status, COUNT(*) FROM direction_predictions GROUP BY status"))
        status = {"last_snapshot_at": now_iso(), "success": True, "database": src.name,
                  "pending": int(counts.get("PENDING", 0)), "evaluated": int(counts.get("EVALUATED", 0))}
        status_path = RUNTIME_DIR / "direction_forward_backup_status.json"
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(status_path)
    return persistent_final


def snapshot_all(include_direction=True):
    cleanup_snapshot_storage()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ok, failed, skipped = [], [], []
    write_status(current_snapshot_started_at=now_iso(), current_snapshot_phase="STARTING",
                 current_snapshot_db=None, current_snapshot_progress={})
    expected = CRITICAL_DBS - ({"direction_forward.sqlite3"} if not include_direction else set())
    for name in sorted(expected):
        if not (RUNTIME_DIR / name).is_file():
            failed.append({"db": name, "error": "FileNotFoundError: critical database missing"})

    # Persist the small direction evidence ledger before potentially large legacy databases.
    for src in sorted(RUNTIME_DIR.glob("*.sqlite3"), key=lambda p: (p.name != "direction_forward.sqlite3", p.name)):
        if not src.is_file():
            continue
        if not include_direction and src.name == "direction_forward.sqlite3":
            continue
        if src.name not in CRITICAL_DBS:
            skipped.append(src.name)
            continue
        last_report = [None, 0.0]

        def report(phase, **details):
            now = time.monotonic()
            if phase != last_report[0] or now - last_report[1] >= 5:
                write_status(current_snapshot_db=src.name, current_snapshot_phase=phase,
                             current_snapshot_progress=details, current_snapshot_progress_at=now_iso())
                last_report[:] = [phase, now]

        try:
            report("PREPARE")
            persist_one(src, stamp, on_progress=report)
            ok.append(src.name)
        except OSError as exc:
            if getattr(exc, "errno", None) == 28:
                cleanup_snapshot_storage()
                try:
                    persist_one(src, stamp, on_progress=report)
                    ok.append(src.name)
                    continue
                except Exception as retry_exc:
                    exc = retry_exc
            failed.append({"db": src.name, "error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            failed.append({"db": src.name, "error": f"{type(exc).__name__}: {exc}"})

    if any(item.get("db") == "direction_forward.sqlite3" for item in failed):
        status_path = RUNTIME_DIR / "direction_forward_backup_status.json"
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last_snapshot_at": now_iso(), "success": False}), encoding="utf-8")
        tmp.replace(status_path)
    cleanup_snapshot_storage()
    write_status(
        current_snapshot_phase="IDLE", current_snapshot_db=None,
        current_snapshot_progress={}, current_snapshot_finished_at=now_iso(),
        last_snapshot_at=now_iso(),
        last_snapshot_success=ok,
        last_snapshot_failed=failed,
        skipped_rebuildable=skipped,
        snapshot_db_count=len(CRITICAL_DBS),
        snapshot_keep_archives=KEEP_ARCHIVES,
        persistent_disk=disk_usage_payload(),
        persistence_status="OK" if ok and not failed else ("PARTIAL" if ok else "ERROR"),
    )
    print("STORAGE_SNAPSHOT", json.dumps({"ok": ok, "failed": failed, "skipped": skipped}, ensure_ascii=False), flush=True)


def snapshot_direction():
    src = RUNTIME_DIR / "direction_forward.sqlite3"
    if not src.is_file():
        return False
    try:
        persist_one(src, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        print("DIRECTION_BACKUP_OK", flush=True)
        return True
    except Exception as exc:
        status_path = RUNTIME_DIR / "direction_forward_backup_status.json"
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last_snapshot_at": now_iso(), "success": False,
                                   "error": type(exc).__name__}), encoding="utf-8")
        tmp.replace(status_path)
        print("DIRECTION_BACKUP_ERROR", type(exc).__name__, flush=True)
        return False


def watch_direction():
    while True:
        try:
            snapshot_direction()
        except Exception as exc:
            print("DIRECTION_BACKUP_STATUS_ERROR", type(exc).__name__, flush=True)
        time.sleep(INTERVAL)


def watch():
    # Purge historical duplicate snapshots immediately, then expose free space
    # before the first scheduled backup attempt.
    try:
        cleanup_snapshot_storage()
        write_status(
            cleanup_at=now_iso(),
            snapshot_keep_archives=KEEP_ARCHIVES,
            persistent_disk=disk_usage_payload(),
        )
    except Exception as exc:
        write_status(last_cleanup_error=f"{type(exc).__name__}: {exc}")
    # An active, large legacy SQLite backup can take a long time. Direction evidence
    # has its own loop, so it continues to persist even if that backup is blocked.
    threading.Thread(target=watch_direction, daemon=True).start()
    time.sleep(45)
    while True:
        try:
            snapshot_all(include_direction=False)
        except Exception as exc:
            write_status(
                current_snapshot_phase="ERROR", current_snapshot_finished_at=now_iso(),
                persistence_status="ERROR",
                persistent_disk=disk_usage_payload(),
                last_snapshot_failed=[{"error": f"{type(exc).__name__}: {exc}"}],
            )
            print("STORAGE_SNAPSHOT_FATAL", type(exc).__name__, exc, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "watch").lower()
    if cmd == "bootstrap":
        bootstrap_runtime()
    elif cmd == "snapshot":
        snapshot_all()
    elif cmd == "watch":
        watch()
    else:
        raise SystemExit(f"unknown command: {cmd}")
