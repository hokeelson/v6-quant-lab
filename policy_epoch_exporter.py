from __future__ import annotations

import json
import time
from pathlib import Path

from src.paths import data_dir
from src.policy_epoch_report import policy_epoch_performance
from src.simulation_db import SimulationDB

POLL_SECONDS = 60
DATA_DIR = Path(data_dir())
DB_PATH = DATA_DIR / "simulation_lab.sqlite3"
OUT_PATH = Path("static") / "policy_epoch_performance.json"


def write_once() -> dict:
    if not DB_PATH.exists():
        payload = {"status": "UNAVAILABLE", "simulation_only": True, "broker_order_api_calls": 0}
    else:
        payload = policy_epoch_performance(SimulationDB(str(DB_PATH)))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(OUT_PATH)
    return payload


def main():
    while True:
        try:
            write_once()
        except Exception:
            pass
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
