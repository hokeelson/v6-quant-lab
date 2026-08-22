from __future__ import annotations
import numpy as np
import pandas as pd
from .metrics import performance_metrics

def aligned_strategy_returns(equity_curves: dict[str, pd.Series]) -> pd.DataFrame:
    cols = {}
    for symbol, eq in equity_curves.items():
        if eq is None or len(eq) < 2:
            continue
        cols[symbol] = eq.pct_change()
    return pd.DataFrame(cols).sort_index()

def inverse_vol_weights(returns: pd.DataFrame, lookback: int = 60, max_weight: float = 0.35) -> pd.DataFrame:
    """
    Ex-ante inverse-volatility weights.
    shift(1) is mandatory: today's allocation uses only information through yesterday.
    """
    vol = returns.rolling(lookback, min_periods=max(20, lookback//2)).std().shift(1)
    inv = 1 / vol.replace(0, np.nan)
    w = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)
    w = w.clip(upper=max_weight)
    # Re-normalize after cap, when there is exposure.
    denom = w.sum(axis=1).replace(0, np.nan)
    return w.div(denom, axis=0).fillna(0.0)

def equal_weights(returns: pd.DataFrame) -> pd.DataFrame:
    available = returns.notna().astype(float)
    denom = available.sum(axis=1).replace(0, np.nan)
    return available.div(denom, axis=0).fillna(0.0)

def portfolio_backtest(
    returns: pd.DataFrame,
    initial_capital: float,
    bars_per_year: int,
    method: str = "inverse_vol",
    rebalance_cost_bps: float = 3.0,
    lookback: int = 60,
    max_weight: float = 0.35,
    rf_annual: float = 0.0
) -> dict:
    r = returns.copy().replace([np.inf,-np.inf], np.nan)
    if method == "inverse_vol":
        w = inverse_vol_weights(r, lookback, max_weight)
    elif method == "equal":
        w = equal_weights(r).shift(1).fillna(0.0)
    else:
        raise ValueError("method must be inverse_vol or equal")

    # A return at t is earned using weights already determined before t.
    gross = (w * r.fillna(0.0)).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    costs = turnover * rebalance_cost_bps / 10000.0
    net = gross - costs
    equity = initial_capital * (1 + net.fillna(0.0)).cumprod()

    m = performance_metrics(equity, pd.DataFrame(), bars_per_year, rf_annual)
    m["avg_turnover"] = float(turnover.mean())
    m["total_rebalance_cost_fraction"] = float(costs.sum())
    return {"returns": net, "weights": w, "equity": equity, "metrics": m}
