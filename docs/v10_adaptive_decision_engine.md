# V10 Adaptive Evidence Decision Engine

V10 replaces the fixed direction formula with a Shadow-only evidence fusion layer.
It does not enable broker orders or short execution.

## Inputs

- Price structure: volatility-normalized 5/20/60-bar returns and EMA structure.
- Regime: uptrend, downtrend, sideways, and high-volatility variants.
- Volume: relative volume, signed price-volume pressure, OBV slope, and breakout confirmation.
- External context: sentiment, market stress, event risk, source confidence, and snapshot freshness.
- Stability: multi-horizon agreement, signal persistence, path efficiency, volatility stability,
  frozen-model OOS stability, and maturing Forward results.

All market features use the closed-bar dataframe supplied by MarketCache. Dashboard fallback
reads cache only and does not call an external market-data API.

## Adaptation rules

Weights depend on the detected regime. High-volatility and sideways conditions assign more
weight to volume and external context; normal trends assign more weight to price structure.
Unavailable volume or external data receives zero weight and the remaining evidence is
renormalized. Missing evidence still lowers coverage and raises the required EV threshold.

The engine chooses `LONG`, `SHORT`, or `NO_TRADE`. A trade is rejected when stability is low,
evidence conflicts, the edge is weak, EV is not decisive, or a mature Forward health record is
paused. It also reports a preferred playbook (`TREND_MOMENTUM`, `CONFIRMED_BREAKOUT`,
`TACTICAL_MEAN_REVERSION`, `EVENT_RISK_DEFENSIVE`, or `WAIT`) for research analysis.

## Stability maturity

OOS calibration is a weak prior. Real Forward trades gain influence gradually until 20 closed
trades. A pair with at least 10 Forward trades and a `PAUSE_CANDIDATE` multiplier can veto a new
direction entry. This prevents a few early wins or losses from causing rapid policy changes.

## Safety and promotion

- Shadow / research only.
- `broker_order_api_calls = 0`.
- `short_execution_enabled = false`.
- No decision is wired to local simulated execution in this change.
- Promotion requires a separate Forward comparison against the prior V9 policy.
- Compare coverage, trade frequency, LONG/SHORT forward reward, NO_TRADE opportunity cost,
  drawdown, and performance by regime before any execution integration.
