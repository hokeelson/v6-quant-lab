from __future__ import annotations

import json
import time
from pathlib import Path

from src.paths import data_dir

POLL_SECONDS = 15
DATA_DIR = Path(data_dir())
STATUS_PATH = DATA_DIR / "storage_persistence_status.json"
PUBLIC_SNAPSHOT_PATH = Path("static") / "research_snapshot.json"

SAFE_KEYS = (
    "mode",
    "persistence_status",
    "last_snapshot_at",
    "last_snapshot_success",
    "last_snapshot_failed",
    "snapshot_interval_seconds",
    "bootstrap_at",
    "restored",
    "bootstrap_warnings",
    "updated_at",
)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_storage_status(raw):
    if not isinstance(raw, dict):
        return {
            "status": "UNAVAILABLE",
            "persistence_status": "UNKNOWN",
        }
    out = {k: raw.get(k) for k in SAFE_KEYS if k in raw}
    out["status"] = "AVAILABLE"
    return out


def export_once():
    storage = _safe_storage_status(_read_json(STATUS_PATH))
    snapshot = _read_json(PUBLIC_SNAPSHOT_PATH)
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("contains_secrets") is not False:
        return False
    if str(snapshot.get("scope") or "") != "PUBLIC_READ_ONLY_RESEARCH_SUMMARY":
        return False

    snapshot["storage_persistence"] = storage
    tmp = PUBLIC_SNAPSHOT_PATH.with_suffix(".json.storage.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PUBLIC_SNAPSHOT_PATH)
    return True


def watch():
    time.sleep(10)
    while True:
        try:
            ok = export_once()
            print("STORAGE_STATUS_EXPORT", "OK" if ok else "WAITING", flush=True)
        except Exception as exc:
            print("STORAGE_STATUS_EXPORT_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    watch()
