from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class TradeEV:
    probability_win: float
    reward_pct: float
    risk_pct: float
    round_trip_cost_pct: float
    expected_value_pct: float
    expected_value_r: float
    evidence_trades: int
    evidence_weight: float

    def as_dict(self) -> dict:
        return asdict(self)


def _finite(value, default=None):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def estimate_trade_ev(
    *,
    oos_metrics: dict | None,
    trade_confidence: float,
    regime_fit: float,
    stop_distance: float,
    target_distance: float,
    buy_cost_rate: float,
    sell_cost_rate: float,
    prior_trades: int = 20,
) -> TradeEV:
    """Conservative observational EV estimate for a candidate entry.

    This function does not authorize an order. It combines a shrunken OOS win-rate
    prior with small current-signal/regime adjustments, then evaluates the planned
    target/stop after estimated round-trip execution costs.
    """
    metrics = oos_metrics if isinstance(oos_metrics, dict) else {}
    n = max(0, int(metrics.get("closed_trades", 0) or 0))
    empirical_win = _finite(metrics.get("win_rate"), 0.5)
    empirical_win = min(1.0, max(0.0, empirical_win))

    prior_n = max(1, int(prior_trades))
    p = (empirical_win * n + 0.5 * prior_n) / (n + prior_n)

    conf = min(100.0, max(0.0, _finite(trade_confidence, 50.0)))
    regime = min(1.0, max(0.0, _finite(regime_fit, 0.5)))
    p += (conf - 50.0) / 100.0 * 0.08
    p += (regime - 0.5) * 0.08
    p = min(0.90, max(0.10, p))

    risk = max(1e-6, _finite(stop_distance, 0.0) or 0.0)
    reward = max(0.0, _finite(target_distance, 0.0) or 0.0)
    cost = max(0.0, _finite(buy_cost_rate, 0.0) or 0.0) + max(
        0.0, _finite(sell_cost_rate, 0.0) or 0.0
    )

    ev_pct = p * reward - (1.0 - p) * risk - cost
    ev_r = ev_pct / risk
    evidence_weight = min(1.0, n / max(1.0, float(prior_n)))

    return TradeEV(
        probability_win=float(p),
        reward_pct=float(reward),
        risk_pct=float(risk),
        round_trip_cost_pct=float(cost),
        expected_value_pct=float(ev_pct),
        expected_value_r=float(ev_r),
        evidence_trades=n,
        evidence_weight=float(evidence_weight),
    )
