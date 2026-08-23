from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir
from src.realtime_layer import RealtimeDB
from src.tca_engine import TCAStore

POLL_SECONDS = 0.5
STATUS_SECONDS = 2.0
MAX_QUOTE_AGE_SECONDS = 15.0
STATUS_PATH = Path(data_dir()) / "tca_status.json"

rt_db = RealtimeDB()
tca = TCAStore(rt_db)
last_status_write = 0.0
state = {
    "status": "STARTING",
    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    "samples": 0,
    "complete_60s": 0,
    "events_created": 0,
    "followups_updated": 0,
    "broker_order_api_calls": 0,
    "message": "TCA worker starting",
}


def _age_seconds(ts):
    try:
        x = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - x.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return 999999.0


def _write_status(force=False):
    global last_status_write
    now = time.time()
    if not force and now - last_status_write < STATUS_SECONDS:
        return
    summary = tca.summary(500)
    state.update({
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "samples": int(summary.get("samples", 0) or 0),
        "complete_60s": int(summary.get("complete_60s", 0) or 0),
        "broker_order_api_calls": 0,
    })
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS_PATH)
    last_status_write = now


print("V6 realtime shadow TCA worker started.", flush=True)
print("TCA analyzes executable bid/ask shadow fills only; broker order API = 0.", flush=True)

while True:
    try:
        signals = {(str(r.get("market")), str(r.get("symbol") or "").upper()): r for r in rt_db.signals()}
        created = updated = 0
        fresh_quotes = 0
        for q in rt_db.quotes():
            market = str(q.get("market") or "")
            symbol = str(q.get("symbol") or "").upper()
            if not market or not symbol or _age_seconds(q.get("ts")) > MAX_QUOTE_AGE_SECONDS:
                continue
            fresh_quotes += 1
            result = tca.observe(market, symbol, signals.get((market, symbol)), q,
                                 max_quote_age_seconds=MAX_QUOTE_AGE_SECONDS)
            created += int(bool(result.get("created")))
            updated += int(result.get("updated", 0) or 0)
        state["events_created"] = int(state.get("events_created", 0)) + created
        state["followups_updated"] = int(state.get("followups_updated", 0)) + updated
        state["status"] = "ONLINE"
        state["message"] = f"TCA active; fresh quotes={fresh_quotes}"
        _write_status()
    except Exception as exc:
        state["status"] = "ERROR"
        state["message"] = f"{type(exc).__name__}: {exc}"
        try:
            _write_status(force=True)
        except Exception:
            pass
        print("TCA_WORKER_ERROR", type(exc).__name__, exc, flush=True)
    time.sleep(POLL_SECONDS)
