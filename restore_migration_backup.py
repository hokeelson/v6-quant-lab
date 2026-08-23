from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _validate_member(name: str) -> str:
    p = Path(name)
    if p.name != name or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"Unsafe archive member: {name}")
    return name


def _restore_sqlite(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(str(src), timeout=30)
    dst_conn = sqlite3.connect(str(dst), timeout=30)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def restore(archive: Path, data_dir: Path) -> dict:
    archive = archive.expanduser().resolve()
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with tempfile.TemporaryDirectory(prefix="v6-restore-") as td:
        tmp = Path(td)
        with zipfile.ZipFile(archive, "r") as zf:
            names = [_validate_member(x.filename) for x in zf.infolist() if not x.is_dir()]
            if "manifest.json" not in names:
                raise ValueError("Not a V6 migration backup: manifest.json is missing")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != "v6-oracle-migration-v1":
                raise ValueError(f"Unsupported backup format: {manifest.get('format')}")
            zf.extractall(tmp)

        restored = []
        for name in manifest.get("databases") or []:
            _validate_member(name)
            if not str(name).endswith(".sqlite3"):
                continue
            src = tmp / name
            if not src.exists():
                raise ValueError(f"Backup database missing: {name}")
            dst = data_dir / name
            if dst.exists() and dst.stat().st_size:
                safety = data_dir / f"{name}.pre_restore_{stamp}"
                shutil.copy2(dst, safety)
            _restore_sqlite(src, dst)
            restored.append(name)

    return {"restored": restored, "data_dir": str(data_dir), "backup_created_at": manifest.get("created_at")}


def main():
    parser = argparse.ArgumentParser(description="Restore a V6 Railway migration ZIP into an Oracle VM data directory.")
    parser.add_argument("archive", help="Path to v6_oracle_migration_*.zip")
    parser.add_argument("data_dir", nargs="?", default="/opt/v6-data", help="Target V6 data directory")
    args = parser.parse_args()
    result = restore(Path(args.archive), Path(args.data_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
