from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path


def checkpoint_shadow_db(path: str) -> bool:
    """Atomically checkpoint the V2 ledger into the Railway rescue snapshot.

    The V2 runtime DB remains isolated in the runtime directory. This function
    only writes a validated SQLite backup to the same persistent rescue location
    used by the production simulator; it never touches simulation_lab.sqlite3.
    """
    persist_root = os.getenv("V6_PERSISTENT_DATA_DIR")
    if not persist_root:
        return False

    src = Path(path)
    if src.name != "crypto_v2_shadow.sqlite3" or not src.exists():
        return False

    current = Path(persist_root) / "v6-snapshots" / "current"
    current.mkdir(parents=True, exist_ok=True)
    target = current / src.name
    tmp = current / f".{src.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    src_con = dst_con = None
    try:
        src_con = sqlite3.connect(str(src), timeout=30)
        dst_con = sqlite3.connect(str(tmp), timeout=30)
        src_con.backup(dst_con, pages=256, sleep=0.01)
        dst_con.commit()
        row = dst_con.execute("PRAGMA quick_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise sqlite3.DatabaseError("Crypto V2 checkpoint quick_check failed")
        dst_con.close()
        dst_con = None
        src_con.close()
        src_con = None
        os.replace(tmp, target)
        return True
    except Exception:
        return False
    finally:
        if dst_con is not None:
            dst_con.close()
        if src_con is not None:
            src_con.close()
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
