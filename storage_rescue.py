from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PERSIST_DIR = Path(os.getenv("V6_PERSISTENT_DATA_DIR", "/data"))
RUNTIME_DIR = Path(os.getenv("V6_RUNTIME_DATA_DIR", "/tmp/v6-data-runtime"))
SNAPSHOT_DIR = PERSIST_DIR / "v6-snapshots"
CURRENT_DIR = SNAPSHOT_DIR / "current"
ARCHIVE_DIR = SNAPSHOT_DIR / "archive"
STATUS_PATH = RUNTIME_DIR / "storage_persistence_status.json"
INTERVAL = max(60, int(os.getenv("V6_SNAPSHOT_INTERVAL_SECONDS", "300")))
KEEP_ARCHIVES = max(2, int(os.getenv("V6_SNAPSHOT_KEEP", "6")))


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


def quick_check(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        uri = f"file:{path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
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


def bootstrap_runtime():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    names = set()
    for base in (PERSIST_DIR, CURRENT_DIR):
        try:
            names.update(p.name for p in base.glob("*.sqlite3") if p.is_file())
        except Exception:
            pass

    restored = []
    warnings = []
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
        persistent_dir=str(PERSIST_DIR),
        runtime_dir=str(RUNTIME_DIR),
    )
    print("STORAGE_BOOTSTRAP", json.dumps({"restored": restored, "warnings": warnings}, ensure_ascii=False), flush=True)


def sqlite_backup(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    src_con = sqlite3.connect(str(src), timeout=30)
    dst_con = sqlite3.connect(str(tmp), timeout=30)
    try:
        src_con.backup(dst_con, pages=256, sleep=0.02)
        dst_con.commit()
    finally:
        dst_con.close()
        src_con.close()
    if not quick_check(tmp):
        tmp.unlink(missing_ok=True)
        raise sqlite3.DatabaseError("snapshot quick_check failed")
    tmp.replace(dst)


def persist_one(src: Path, stamp: str):
    local_stage = RUNTIME_DIR / ".snapshot-stage" / src.name
    local_stage.parent.mkdir(parents=True, exist_ok=True)
    sqlite_backup(src, local_stage)

    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    persistent_tmp = CURRENT_DIR / (src.name + ".new")
    shutil.copy2(local_stage, persistent_tmp)
    persistent_final = CURRENT_DIR / src.name
    persistent_tmp.replace(persistent_final)

    archive = ARCHIVE_DIR / f"{stamp}__{src.name}"
    try:
        shutil.copy2(local_stage, archive)
    except Exception:
        # Current snapshot is the important copy. Archive failure is non-fatal.
        pass
    return persistent_final


def prune_archives():
    try:
        groups = {}
        for p in ARCHIVE_DIR.glob("*__*.sqlite3"):
            name = p.name.split("__", 1)[1]
            groups.setdefault(name, []).append(p)
        for _, files in groups.items():
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[KEEP_ARCHIVES:]:
                try:
                    old.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def snapshot_all():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ok, failed = [], []
    for src in sorted(RUNTIME_DIR.glob("*.sqlite3")):
        if not src.is_file():
            continue
        try:
            persist_one(src, stamp)
            ok.append(src.name)
        except Exception as exc:
            failed.append({"db": src.name, "error": f"{type(exc).__name__}: {exc}"})
    prune_archives()
    write_status(
        last_snapshot_at=now_iso(),
        last_snapshot_success=ok,
        last_snapshot_failed=failed,
        persistence_status="OK" if ok and not failed else ("PARTIAL" if ok else "ERROR"),
    )
    print("STORAGE_SNAPSHOT", json.dumps({"ok": ok, "failed": failed}, ensure_ascii=False), flush=True)


def watch():
    # Let workers initialize DB schemas before the first snapshot.
    time.sleep(45)
    while True:
        try:
            snapshot_all()
        except Exception as exc:
            write_status(persistence_status="ERROR", last_snapshot_failed=[{"error": f"{type(exc).__name__}: {exc}"}])
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
