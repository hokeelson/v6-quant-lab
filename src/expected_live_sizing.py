from __future__ import annotations

import time

from .expected_live_deviation import expected_live_deviation_snapshot

CACHE_SECONDS = 60.0
MIN_LIVE_TRADES = 5
_CACHE = {"path": None, "at": 0.0, "snapshot": None}


def _snapshot(db) -> dict:
    now = time.monotonic()
    path = str(getattr(db, "path", ""))
    cached = _CACHE.get("snapshot")
    if cached is not None and _CACHE.get("path") == path and now - float(_CACHE.get("at") or 0.0) < CACHE_SECONDS:
        return cached
    snap = expected_live_deviation_snapshot(db)
    _CACHE.update({"path": path, "at": now, "snapshot": snap})
    return snap


def expected_live_sizing_assessment(db, market: str, symbol: str, horizon: str, strategy: str) -> dict:
    """Return the active sizing multiplier for OOS-vs-forward divergence.

    This is deliberately fail-safe at the caller and deliberately inactive until
    at least five forward closed trades exist for the exact market/symbol/horizon/
    strategy combination. The full diagnostic snapshot is cached for 60 seconds so
    repeated entry checks do not rescan the simulation ledger unnecessarily.
    """
    key = f"{market}:{str(symbol or '').upper()}:{horizon}:{strategy}"
    out = {
        "expected_live_multiplier": 1.0,
        "expected_live_state": "LEARNING",
        "expected_live_samples": 0,
        "expected_live_deviation_score": None,
        "expected_live_reasons": [],
        "expected_live_performance_key": key,
        "expected_live_evidence_weight": 0.0,
    }

    snap = _snapshot(db)
    row = next(
        (r for r in (snap.get("rows") or []) if str(r.get("performance_key") or "") == key),
        None,
    )
    if not row:
        return out

    samples = int(row.get("live_closed_trades", 0) or 0)
    state = str(row.get("state") or "LEARNING")
    suggested = float(row.get("suggested_confidence_multiplier", 1.0) or 1.0)
    multiplier = suggested if samples >= MIN_LIVE_TRADES and state != "LEARNING" else 1.0

    out.update({
        "expected_live_multiplier": max(0.0, min(1.0, multiplier)),
        "expected_live_state": state,
        "expected_live_samples": samples,
        "expected_live_deviation_score": row.get("deviation_score"),
        "expected_live_reasons": list(row.get("reasons") or []),
        "expected_live_performance_key": str(row.get("performance_key") or key),
        "expected_live_evidence_weight": float(row.get("evidence_weight", 0.0) or 0.0),
    })
    return out
