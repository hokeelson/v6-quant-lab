from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


META_KEY = "bidirectional_registered_at"


def _utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def ensure_registration(db) -> str:
    """Persist the wall-clock instant when this forward shadow first became active."""
    with db._c() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS bidirectional_meta(
                 key TEXT PRIMARY KEY,
                 value TEXT NOT NULL
               )"""
        )
        row = c.execute("SELECT value FROM bidirectional_meta WHERE key=?", (META_KEY,)).fetchone()
        if row:
            return str(row[0])
        stamp = datetime.now(timezone.utc).isoformat()
        c.execute("INSERT INTO bidirectional_meta(key,value) VALUES(?,?)", (META_KEY, stamp))
        return stamp


def is_forward_eligible(db, decision_available_at) -> bool:
    """Only decisions whose next executable open occurs after registration are valid."""
    registered_at = ensure_registration(db)
    return _utc(decision_available_at) >= _utc(registered_at)


def registration_state(db) -> dict:
    registered_at = ensure_registration(db)
    return {
        "registered_at": registered_at,
        "forward_only": True,
        "historical_backfill_allowed": False,
    }
