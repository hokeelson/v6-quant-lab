from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _market_from_account(account_id: str) -> str:
    aid = str(account_id or "")
    return aid.rsplit("_", 1)[0] if "_" in aid else aid


def _loss_streak(values: list[float]) -> int:
    best = cur = 0
    for value in values:
        if value < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _row(group: pd.DataFrame, market: str, symbol: str, horizon: str, strategy: str) -> dict:
    group = group.sort_values("_exit_time").tail(20).copy()
    n = int(len(group))
    returns = pd.to_numeric(group["return_pct"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    pnls = pd.to_numeric(group["realized_pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float)

    wins = int((pnls > 0).sum())
    gross_profit = float(pnls[pnls > 0].sum())
    gross_loss = float(-pnls[pnls < 0].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else math.nan)

    age = np.arange(n - 1, -1, -1, dtype=float)
    weights = np.power(0.5, age / 6.0)
    weights = weights / weights.sum() if weights.sum() > 0 else np.ones(n) / max(n, 1)
    weighted_avg_return = float(np.sum(returns * weights)) if n else 0.0
    weighted_win_rate = float(np.sum((pnls > 0).astype(float) * weights)) if n else 0.0
    streak = _loss_streak(returns.tolist())

    failure_votes = 0
    if n >= 3 and wins == 0:
        failure_votes += 1
    if n >= 5 and weighted_win_rate < 0.35:
        failure_votes += 1
    if n >= 5 and np.isfinite(profit_factor) and profit_factor < 0.80:
        failure_votes += 1
    if n >= 5 and weighted_avg_return < 0:
        failure_votes += 1
    if n >= 5 and streak >= 3:
        failure_votes += 1

    # Symbol-level evidence is intentionally conservative. It can reduce future
    # virtual entry size, but never changes the strategy signal or broker behavior.
    if n < 3:
        state, multiplier = "LEARNING", 1.00
    elif n >= 10 and failure_votes >= 4:
        state, multiplier = "PAUSE_CANDIDATE", 0.25
    elif n >= 5 and failure_votes >= 3:
        state, multiplier = "SHADOW_ONLY_CANDIDATE", 0.50
    elif failure_votes >= 1:
        state, multiplier = "WATCH", 0.75
    else:
        state, multiplier = "NORMAL", 1.00

    return {
        "market": market,
        "symbol": symbol,
        "horizon": horizon,
        "strategy": strategy,
        "samples": n,
        "wins": wins,
        "losses": int((pnls < 0).sum()),
        "weighted_win_rate": weighted_win_rate,
        "weighted_avg_return": weighted_avg_return,
        "profit_factor": float(profit_factor) if np.isfinite(profit_factor) else None,
        "profit_factor_infinite": bool(np.isinf(profit_factor)),
        "max_loss_streak": streak,
        "failure_votes": failure_votes,
        "state": state,
        "shadow_weight_multiplier": multiplier,
        "performance_key": f"{market}:{symbol}:{horizon}:{strategy}",
        "shadow_only": True,
    }


def symbol_strategy_health_snapshot(db) -> dict:
    trades = pd.DataFrame(db.recent_trades(5000))
    if trades.empty:
        return {"symbols": [], "shadow_only": True}

    trades["market"] = trades["account_id"].map(_market_from_account)
    trades["symbol"] = trades["symbol"].astype(str).str.upper()
    trades["strategy"] = trades["strategy"].fillna("UNKNOWN").astype(str)
    trades["horizon"] = trades["horizon"].fillna("").astype(str)
    trades["_exit_time"] = pd.to_datetime(trades["exit_bar"], utc=True, errors="coerce")
    trades = trades.dropna(subset=["_exit_time"])

    rows = []
    for (market, symbol, horizon, strategy), group in trades.groupby(
        ["market", "symbol", "horizon", "strategy"], dropna=False
    ):
        rows.append(_row(group, str(market), str(symbol).upper(), str(horizon), str(strategy)))

    rows.sort(key=lambda item: (item["shadow_weight_multiplier"], -item["samples"], item["performance_key"]))
    return {"symbols": rows, "shadow_only": True}


def find_symbol_strategy_health(
    snapshot: dict, market: str, symbol: str, horizon: str, strategy: str
) -> dict | None:
    target_symbol = str(symbol or "").upper()
    target_strategy = str(strategy or "")
    for row in snapshot.get("symbols") or []:
        if str(row.get("market") or "") != str(market or ""):
            continue
        if str(row.get("symbol") or "").upper() != target_symbol:
            continue
        if str(row.get("horizon") or "") != str(horizon or ""):
            continue
        if str(row.get("strategy") or "") != target_strategy:
            continue
        return row
    return None
