from __future__ import annotations

import time

from .expected_live_deviation import expected_live_deviation_snapshot

CACHE_SECONDS = 60.0
MIN_LIVE_TRADES = 5
QUARANTINE_MIN_TRADES = 12
MAX_FORWARD_WEIGHT = 0.90
FORWARD_DOMINANCE_TRADES = 30
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


def forward_shadow_weight(samples: int) -> float:
    n = max(0, int(samples or 0))
    if n < MIN_LIVE_TRADES:
        return 0.0
    if n >= FORWARD_DOMINANCE_TRADES:
        return MAX_FORWARD_WEIGHT
    span = FORWARD_DOMINANCE_TRADES - MIN_LIVE_TRADES
    progress = (n - MIN_LIVE_TRADES) / max(1, span)
    return 0.25 + (MAX_FORWARD_WEIGHT - 0.25) * progress


def blend_expected_live_multiplier(suggested: float, samples: int) -> tuple[float, float, float]:
    suggested = max(0.0, min(1.0, float(suggested or 1.0)))
    forward_weight = forward_shadow_weight(samples)
    backtest_weight = 1.0 - forward_weight
    effective = backtest_weight * 1.0 + forward_weight * suggested
    return max(0.0, min(1.0, effective)), forward_weight, backtest_weight


def _should_quarantine(row: dict) -> bool:
    samples = int(row.get("live_closed_trades", 0) or 0)
    reasons = set(row.get("reasons") or [])
    return (
        samples >= QUARANTINE_MIN_TRADES
        and str(row.get("state") or "") == "SEVERE_DIVERGENCE"
        and "OOS_POSITIVE_LIVE_NEGATIVE" in reasons
        and "EXPECTANCY_SIGN_REVERSAL" in reasons
        and "PROFIT_FACTOR_DETERIORATION" in reasons
    )


def expected_live_sizing_assessment(db, market: str, symbol: str, horizon: str, strategy: str) -> dict:
    key = f"{market}:{str(symbol or '').upper()}:{horizon}:{strategy}"
    out = {
        "expected_live_multiplier": 1.0,
        "expected_live_state": "LEARNING",
        "expected_live_samples": 0,
        "expected_live_deviation_score": None,
        "expected_live_reasons": [],
        "expected_live_performance_key": key,
        "expected_live_evidence_weight": 0.0,
        "forward_shadow_weight": 0.0,
        "backtest_oos_weight": 1.0,
        "raw_expected_live_multiplier": 1.0,
        "quarantined": False,
        "quarantine_reason": None,
    }

    snap = _snapshot(db)
    row = next((r for r in (snap.get("rows") or []) if str(r.get("performance_key") or "") == key), None)
    if not row:
        return out

    samples = int(row.get("live_closed_trades", 0) or 0)
    state = str(row.get("state") or "LEARNING")
    suggested = float(row.get("suggested_confidence_multiplier", 1.0) or 1.0)
    quarantined = _should_quarantine(row)

    if quarantined:
        # Keep a 25% research allocation rather than deleting the signal entirely.
        # This is a quarantine: it sharply limits new Shadow exposure while still
        # collecting evidence that can later support recovery.
        multiplier = 0.25
        forward_weight = forward_shadow_weight(samples)
        backtest_weight = 1.0 - forward_weight
        state = "QUARANTINED"
    elif samples >= MIN_LIVE_TRADES and state != "LEARNING":
        multiplier, forward_weight, backtest_weight = blend_expected_live_multiplier(suggested, samples)
    else:
        multiplier, forward_weight, backtest_weight = 1.0, 0.0, 1.0

    out.update({
        "expected_live_multiplier": multiplier,
        "expected_live_state": state,
        "expected_live_samples": samples,
        "expected_live_deviation_score": row.get("deviation_score"),
        "expected_live_reasons": list(row.get("reasons") or []),
        "expected_live_performance_key": str(row.get("performance_key") or key),
        "expected_live_evidence_weight": float(row.get("evidence_weight", 0.0) or 0.0),
        "forward_shadow_weight": forward_weight,
        "backtest_oos_weight": backtest_weight,
        "raw_expected_live_multiplier": max(0.0, min(1.0, suggested)),
        "quarantined": quarantined,
        "quarantine_reason": "MATURE_EXPECTANCY_SIGN_REVERSAL" if quarantined else None,
    })
    return out
