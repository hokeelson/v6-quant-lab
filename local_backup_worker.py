from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.paths import data_dir

CHECK_SECONDS = 3600
RETENTION_DAYS = 14


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_sqlite_backup(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    src_con = dst_con = None
    try:
        src_con = sqlite3.connect(str(src), timeout=30)
        dst_con = sqlite3.connect(str(tmp), timeout=30)
        src_con.backup(dst_con, pages=256, sleep=0.01)
        dst_con.commit()
        row = dst_con.execute("PRAGMA quick_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError("backup quick_check failed")
        dst_con.close(); dst_con = None
        src_con.close(); src_con = None
        os.replace(tmp, dst)
        return True
    finally:
        if dst_con is not None:
            dst_con.close()
        if src_con is not None:
            src_con.close()
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def run_backup(root: Path | None = None) -> dict:
    root = Path(root or data_dir())
    backup_root = root / "backups"
    daily = backup_root / _today()
    daily.mkdir(parents=True, exist_ok=True)

    copied = []
    for src in sorted(root.glob("*.sqlite3")):
        try:
            if _safe_sqlite_backup(src, daily / src.name):
                copied.append(src.name)
        except Exception:
            pass

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(root),
        "backup_dir": str(daily),
        "sqlite_files": copied,
    }
    (daily / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)
    for child in backup_root.iterdir() if backup_root.exists() else []:
        if not child.is_dir():
            continue
        try:
            day = datetime.strptime(child.name, "%Y-%m-%d").date()
        except Exception:
            continue
        if day < cutoff:
            shutil.rmtree(child, ignore_errors=True)
    return manifest


def main():
    last_day = None
    while True:
        day = _today()
        if day != last_day:
            try:
                result = run_backup()
                last_day = day
                print(
                    "LOCAL_BACKUP OK",
                    result.get("backup_dir"),
                    f"files={len(result.get('sqlite_files') or [])}",
                    flush=True,
                )
            except Exception as exc:
                print("LOCAL_BACKUP_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
