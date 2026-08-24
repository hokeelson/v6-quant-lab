from __future__ import annotations

import json
import time
from pathlib import Path

from src.paths import data_dir, db_path
from src.trial_ledger import TrialLedger

POLL_SECONDS = 60
DATA_DIR = Path(data_dir())
ledger = TrialLedger(db_path("trial_ledger.sqlite3"))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


print("V6 Trial Ledger worker started. Side-channel audit only.", flush=True)
while True:
    try:
        gov = ledger.sync_governance(db_path("model_governance.sqlite3"))
        worker = _read_json(DATA_DIR / "worker_status.json")
        quality = _read_json(DATA_DIR / "data_quality_status.json")
        ledger.sync_worker_cycle(worker, quality)
        print("TRIAL_LEDGER_SYNC", gov, ledger.summary(), flush=True)
    except Exception as exc:
        print("TRIAL_LEDGER_ERROR", type(exc).__name__, exc, flush=True)
    time.sleep(POLL_SECONDS)
