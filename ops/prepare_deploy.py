"""Explicit operator-run maintenance; never invoked by application startup.

Keep the dashboard tab closed. This does not stop PID 1 or SSH. A separate
guardian resumes the identified processes after 15 minutes, even on disconnect.
This tool prepares restore files; it never merges, deploys, or submits orders.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROGRAMS = (
    "worker_supervisor_v8.py", "realtime_supervisor.py", "tca_supervisor.py",
    "crypto_v2_shadow_supervisor.py", "direction_shadow_supervisor.py",
    "storage_rescue.py", "trial_ledger_worker.py", "storage_status_exporter.py",
    "runtime_health_exporter.py", "policy_epoch_exporter.py",
    "external_intelligence_worker.py", "live_worker_v8.py", "realtime_worker.py",
    "tca_worker.py", "crypto_v2_shadow_worker.py", "direction_shadow_worker.py",
)
DATABASES = (
    "simulation_lab.sqlite3", "data_quality.sqlite3", "model_governance.sqlite3",
    "trial_ledger.sqlite3", "direction_forward.sqlite3", "forward_validation.sqlite3",
    "crypto_v2_shadow.sqlite3", "realtime_execution.sqlite3",
)
LEASE_SECONDS = 900
WORK_SECONDS = 300


def process_state(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], fields[19]


def discover():
    found = {}
    for proc in Path("/proc").glob("[0-9]*"):
        pid = int(proc.name)
        if pid in (1, os.getpid()):
            continue
        try:
            args = proc.joinpath("cmdline").read_bytes().decode().split("\0")
            for name in {Path(arg).name for arg in args} & set(PROGRAMS):
                if name in found:
                    raise RuntimeError(f"duplicate process: {name}")
                state, started = process_state(pid)
                if state in ("T", "t", "Z"):
                    raise RuntimeError(f"process not ready: {name} ({state})")
                found[name] = {"pid": pid, "started": started, "name": name}
        except (FileNotFoundError, ProcessLookupError):
            continue
    missing = set(PROGRAMS) - set(found)
    if missing:
        raise RuntimeError(f"processes missing: {sorted(missing)}")
    return [found[name] for name in PROGRAMS]


def resume(items):
    errors = []
    for item in reversed(items):
        try:
            _, started = process_state(item["pid"])
            if started == item["started"]:
                os.kill(item["pid"], signal.SIGCONT)
        except (FileNotFoundError, ProcessLookupError):
            pass
        except OSError as exc:
            errors.append(f'{item["pid"]}: {type(exc).__name__}')
    return errors


def guard(items, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("maintenance work budget exceeded")
    for item in items:
        state, started = process_state(item["pid"])
        if started != item["started"] or state not in ("T", "t"):
            raise RuntimeError(f'process no longer paused: {item["name"]}')


def digest(path, check=lambda: None):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024**2), b""):
            check()
            result.update(block)
    return result.hexdigest()


def counts(path, deadline):
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as con:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        con.execute("BEGIN")
        names = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        return {name: con.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for (name,) in names.fetchall()}


def prepare(module_path):
    spec = importlib.util.spec_from_file_location("maintenance_storage", module_path)
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    # SSH shells do not inherit cloud_start.sh's exported directory variables.
    backup.PERSIST_DIR = Path("/data")
    backup.RUNTIME_DIR = Path("/tmp/v6-data-runtime")
    backup.SNAPSHOT_DIR = backup.PERSIST_DIR / "v6-snapshots"
    backup.CURRENT_DIR = backup.SNAPSHOT_DIR / "current"
    backup.ARCHIVE_DIR = backup.SNAPSHOT_DIR / "archive"
    backup.STATUS_PATH = backup.RUNTIME_DIR / "storage_persistence_status.json"
    backup.KEEP_ARCHIVES = 0
    backup.CRITICAL_DBS = set(DATABASES)
    for name in DATABASES:
        if not (backup.RUNTIME_DIR / name).is_file():
            raise FileNotFoundError(f"critical source missing: {name}")
    items = discover()  # All preflight checks precede pausing.
    folder = Path(tempfile.mkdtemp(prefix="pr22-maintenance-", dir="/tmp"))
    manifest = folder / "lease.json"
    lease = {"items": items, "created_at": time.time(), "seconds": LEASE_SECONDS}
    manifest.write_text(json.dumps(lease))
    guardian = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--resume", str(manifest), "--wait"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + WORK_SECONDS
    try:
        if guardian.poll() is not None:
            raise RuntimeError("recovery guardian did not start")
        for item in items:
            if process_state(item["pid"])[1] != item["started"]:
                raise RuntimeError("process identity changed")
            os.kill(item["pid"], signal.SIGSTOP)
        # Bound the kernel group-stop transition; no user-paced pause step.
        for attempt in range(20):
            try:
                guard(items, deadline)
                break
            except RuntimeError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
        print("PAUSED; capturing all eight databases now", flush=True)
        receipt = {}
        for name in DATABASES:
            guard(items, deadline)
            src = backup.RUNTIME_DIR / name
            dst = backup.CURRENT_DIR / name
            # Atomic base-file replacement must never strand committed target WAL data.
            wal = Path(str(dst) + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise RuntimeError(f"restore WAL is nonempty; manual review required: {name}")
            expected = counts(src, deadline)
            last_phase = [None]

            def progress(phase, **details):
                guard(items, deadline)
                if phase != last_phase[0]:
                    print(name, phase, flush=True)
                    last_phase[0] = phase

            backup.persist_one(src, "maintenance", on_progress=progress)
            guard(items, deadline)
            actual = counts(dst, deadline)
            if actual != expected or counts(src, deadline) != expected:
                raise RuntimeError(f"table counts changed or differ: {name}")
            stage = backup.RUNTIME_DIR / ".snapshot-stage" / name
            fingerprint = digest(stage, lambda: guard(items, deadline))
            if digest(dst, lambda: guard(items, deadline)) != fingerprint:
                raise RuntimeError(f"snapshot checksum mismatch: {name}")
            receipt[name] = {"counts": actual, "sha256": fingerprint, "bytes": dst.stat().st_size}
            print(name, "VERIFIED", flush=True)
        guard(items, deadline)
        verified_at = datetime.now(timezone.utc).isoformat()
        backup.write_status(last_snapshot_at=verified_at, last_snapshot_success=list(DATABASES),
                            last_snapshot_failed=[], persistence_status="OK",
                            current_snapshot_phase="IDLE", current_snapshot_db=None)
        # Small durable receipt, not another full-size database copy.
        saved = backup.PERSIST_DIR / f"{folder.name}-receipt.json"
        with saved.open("x") as f:
            json.dump({"verified_at": verified_at, "databases": receipt}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        guard(items, deadline)
        remaining = int(lease["created_at"] + LEASE_SECONDS - time.time())
        print("READY_FOR_DEPLOY", flush=True)
        print("Verified UTC:", verified_at)
        print("Main ledger:", json.dumps(receipt["simulation_lab.sqlite3"]["counts"]))
        print("Receipt:", saved)
        print("Resume command:", sys.executable, Path(__file__).resolve(), "--resume", manifest)
        print("Automatic resume in seconds:", remaining, flush=True)
    except BaseException:
        errors = resume(items)
        print("ABORTED: recovery signals sent; do not deploy.", errors, flush=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-module", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if args.resume:
        lease = json.loads(args.resume.read_text())
        if args.wait:
            remaining = max(0, min(LEASE_SECONDS, lease["created_at"] + lease["seconds"] - time.time()))
            deadline = time.monotonic() + remaining
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1, remaining))
        errors = resume(lease["items"])
        print("RECOVERY_SIGNALS_SENT", errors)
        return 1 if errors else 0
    if not args.backup_module:
        parser.error("--backup-module or --resume is required")
    prepare(args.backup_module)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
