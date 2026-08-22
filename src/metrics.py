from __future__ import annotations
import math
import numpy as np
import pandas as pd

def max_drawdown(equity: pd.Series) -> float:
    if len(equity) == 0:
        return float("nan")
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())

def _period_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().replace([np.inf,-np.inf], np.nan).dropna()

def performance_metrics(equity: pd.Series, trades: pd.DataFrame, bars_per_year: int,
                        risk_free_rate: float = 0.0) -> dict:
    equity = equity.dropna()
    rets = _period_returns(equity)
    if len(equity) < 2:
        return {}
    years = max(len(rets) / bars_per_year, 1 / bars_per_year)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if equity.iloc[0] > 0 else np.nan
    rf_bar = (1 + risk_free_rate) ** (1 / bars_per_year) - 1
    excess = rets - rf_bar
    vol = rets.std(ddof=1)
    sharpe = (excess.mean() / vol) * math.sqrt(bars_per_year) if vol and vol > 0 else np.nan
    downside = rets[rets < rf_bar] - rf_bar
    downside_dev = np.sqrt((downside ** 2).mean()) if len(downside) else np.nan
    sortino = (excess.mean() / downside_dev) * math.sqrt(bars_per_year) if downside_dev and downside_dev > 0 else np.nan
    mdd = max_drawdown(equity)
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan

    if trades is None or len(trades) == 0:
        wins = pd.Series(dtype=float)
        losses = pd.Series(dtype=float)
        closed = pd.DataFrame()
    else:
        closed = trades[trades["action"] == "SELL"].copy()
        pnl = closed["realized_pnl"] if "realized_pnl" in closed else pd.Series(dtype=float)
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]

    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)
    win_rate = float((closed["realized_pnl"] > 0).mean()) if len(closed) else np.nan
    expectancy = float(closed["realized_pnl"].mean()) if len(closed) else np.nan

    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "annual_volatility": float(vol * math.sqrt(bars_per_year)) if pd.notna(vol) else np.nan,
        "sharpe": float(sharpe) if pd.notna(sharpe) else np.nan,
        "sortino": float(sortino) if pd.notna(sortino) else np.nan,
        "max_drawdown": float(mdd),
        "calmar": float(calmar) if pd.notna(calmar) else np.nan,
        "closed_trades": int(len(closed)),
        "win_rate": win_rate,
        "profit_factor": float(profit_factor) if pd.notna(profit_factor) else np.nan,
        "expectancy": expectancy,
    }
