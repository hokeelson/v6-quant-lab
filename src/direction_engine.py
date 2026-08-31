from __future__ import annotations

import math
import numpy as np
import pandas as pd

from .decision_engine import atr, market_regime
from .external_intelligence import external_intelligence_assessment


def _clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-max(-8.0, min(8.0, float(x)))))


def _trend_edge(df: pd.DataFrame) -> float:
    c = pd.to_numeric(df.close, errors="coerce").dropna()
    if len(c) < 70:
        return 0.0
    ret = c.pct_change().dropna()
    vol = float(ret.tail(60).std() or 0.0)
    vol = max(vol, 0.002)
    r5 = float(c.iloc[-1] / c.iloc[-6] - 1.0) if len(c) >= 6 else 0.0
    r20 = float(c.iloc[-1] / c.iloc[-21] - 1.0) if len(c) >= 21 else 0.0
    r60 = float(c.iloc[-1] / c.iloc[-61] - 1.0) if len(c) >= 61 else 0.0
    raw = 0.45 * r5 / (vol * math.sqrt(5)) + 0.35 * r20 / (vol * math.sqrt(20)) + 0.20 * r60 / (vol * math.sqrt(60))
    return _clamp(raw / 2.5, -1.0, 1.0)


def _regime_edge(regime: str) -> float:
    r = str(regime or "")
    if "UP_TREND" in r:
        return 0.75
    if "DOWN_TREND" in r:
        return -0.75
    return 0.0


def assess_direction(df: pd.DataFrame, market: str, strategy: str, stop_distance: float, target_distance: float) -> dict:
    """Return LONG / SHORT / NO_TRADE research direction.

    This is a bidirectional Shadow research layer. It does not submit orders. Long and
    short EV values are directional EV proxies used for comparison until enough
    forward short evidence exists for independent calibration.
    """
    regime = market_regime(df)
    trend = _trend_edge(df)
    regime_edge = _regime_edge(regime)
    ext = external_intelligence_assessment(market, strategy)
    sentiment = _clamp(ext.get("external_sentiment_score", 0.0), -1.0, 1.0)
    risk = _clamp(ext.get("external_risk_score", 0.0), 0.0, 1.0)
    event = _clamp(ext.get("external_event_risk", 0.0), 0.0, 1.0)
    confidence_external = _clamp(ext.get("external_confidence", 0.0), 0.0, 1.0)

    # Risk-off is a short bias, not an automatic short signal. Trend/regime still dominate.
    external_edge = 0.45 * sentiment - 0.35 * risk - 0.20 * event
    edge = _clamp(0.50 * trend + 0.30 * regime_edge + 0.20 * external_edge, -1.0, 1.0)

    p_long = _clamp(_sigmoid(2.4 * edge), 0.10, 0.90)
    p_short = 1.0 - p_long
    stop = max(0.005, float(stop_distance or 0.03))
    target = max(stop, float(target_distance or stop * 1.5))
    rr = _clamp(target / stop, 0.75, 4.0)
    long_ev_r = p_long * rr - (1.0 - p_long)
    short_ev_r = p_short * rr - (1.0 - p_short)
    gap = abs(long_ev_r - short_ev_r)

    min_ev = 0.10
    min_gap = 0.12
    if long_ev_r >= min_ev and long_ev_r - short_ev_r >= min_gap:
        direction = "LONG"
    elif short_ev_r >= min_ev and short_ev_r - long_ev_r >= min_gap:
        direction = "SHORT"
    else:
        direction = "NO_TRADE"

    confidence = _clamp(0.45 + 0.35 * abs(edge) + 0.20 * confidence_external, 0.0, 1.0)
    return {
        "direction": direction,
        "direction_confidence": confidence,
        "long_ev_proxy_r": float(long_ev_r),
        "short_ev_proxy_r": float(short_ev_r),
        "ev_gap_r": float(gap),
        "direction_edge": float(edge),
        "trend_edge": float(trend),
        "regime_edge": float(regime_edge),
        "external_edge": float(external_edge),
        "regime": regime,
        "external_risk_regime": ext.get("external_risk_regime"),
        "external_sentiment_score": sentiment,
        "external_risk_score": risk,
        "external_event_risk": event,
        "shadow_only": True,
        "broker_order_api_calls": 0,
        "short_execution_enabled": False,
        "ev_type": "DIRECTIONAL_PROXY_UNTIL_SHORT_FORWARD_CALIBRATION",
    }
