from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .paths import data_dir


def _sqlite_backup(src: Path, dst: Path) -> None:
    """Create a transactionally consistent SQLite copy while the app is running."""
    src_conn = sqlite3.connect(str(src), timeout=30)
    dst_conn = sqlite3.connect(str(dst), timeout=30)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def build_migration_backup() -> tuple[bytes, dict]:
    """Return a ZIP containing all V6 SQLite databases plus a manifest.

    Credentials are environment variables and are intentionally not included.
    SQLite's backup API is used so WAL databases can be copied safely while workers run.
    """
    root = Path(data_dir())
    dbs = sorted(p for p in root.glob("*.sqlite3") if p.is_file())
    manifest = {
        "format": "v6-oracle-migration-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_data_dir": str(root),
        "databases": [p.name for p in dbs],
        "credentials_included": False,
    }

    out = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="v6-migrate-") as td:
        tmp = Path(td)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for src in dbs:
                dst = tmp / src.name
                _sqlite_backup(src, dst)
                zf.write(dst, arcname=src.name)
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return out.getvalue(), manifest
