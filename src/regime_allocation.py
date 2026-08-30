from __future__ import annotations


def regime_strategy_multiplier(strategy: str, regime: str) -> float:
    """Conservative strategy allocation prior for the current market regime.

    The multiplier is bounded to 0.70..1.00. It never increases above the
    model-requested size and never blocks Shadow learning by itself.
    """
    s = str(strategy or "")
    r = str(regime or "UNKNOWN")
    if r == "UNKNOWN":
        return 0.90

    up = "UP_TREND" in r
    down = "DOWN_TREND" in r
    sideways = "SIDEWAYS" in r
    high_vol = "HIGH_VOL" in r

    if s == "Trend MA":
        score = 1.00 if up else 0.70 if down else 0.82
    elif s == "Momentum":
        score = 0.98 if up else 0.70 if down else 0.78
    elif s == "Mean Reversion RSI":
        if sideways and not high_vol:
            score = 1.00
        elif high_vol:
            score = 0.75
        else:
            score = 0.86
    elif s == "Breakout":
        score = 0.98 if (up or high_vol) else 0.82
    else:
        score = 0.90

    return max(0.70, min(1.00, float(score)))
