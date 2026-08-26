from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd
from .metrics import performance_metrics

@dataclass(frozen=True)
class ExecutionCosts:
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    spread_bps: float = 0.0
    sell_tax_bps: float = 0.0

    @property
    def buy_rate(self) -> float:
        return (self.commission_bps + self.slippage_bps + self.spread_bps / 2.0) / 10000.0

    @property
    def sell_rate(self) -> float:
        return (self.commission_bps + self.slippage_bps + self.spread_bps / 2.0 + self.sell_tax_bps) / 10000.0

    @property
    def one_way_rate(self) -> float:
        # Compatibility value for callers that still require a symmetric rate.
        return (self.buy_rate + self.sell_rate) / 2.0

@dataclass(frozen=True)
class RiskRules:
    max_position_pct: float = 0.20
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20

def run_backtest(df: pd.DataFrame, signal: pd.Series, initial_capital: float,
                 costs: ExecutionCosts, risk: RiskRules, bars_per_year: int,
                 risk_free_rate: float = 0.0) -> dict:
    """
    Long-only, one-symbol event backtest.
    Critical anti-lookahead rule:
    signal[t] is observed after bar t; order executes at bar t+1 OPEN.
    Stops/targets are checked using the subsequent bar high/low after entry.
    """
    data = df.copy().dropna()
    sig = signal.reindex(data.index).fillna(0.0).astype(float)
    if len(data) < 3:
        raise ValueError("Not enough bars.")

    cash = float(initial_capital)
    qty = 0.0
    entry_fill = np.nan
    entry_cost_basis = 0.0
    trades = []
    equity_vals = [cash]
    equity_idx = [data.index[0]]
    buy_cost_rate = costs.buy_rate
    sell_cost_rate = costs.sell_rate

    # pending target desired after prior bar close: 0 or 1
    pending_target = int(sig.iloc[0] > 0)

    for i in range(1, len(data)):
        ts = data.index[i]
        o, h, l, c = map(float, [data["open"].iloc[i], data["high"].iloc[i], data["low"].iloc[i], data["close"].iloc[i]])

        # Execute prior signal at current OPEN.
        if pending_target == 1 and qty == 0:
            fill = o * (1 + buy_cost_rate)
            allocation = min(cash, initial_capital * risk.max_position_pct)
            new_qty = allocation / fill if fill > 0 else 0.0
            if new_qty > 0:
                spent = new_qty * fill
                cash -= spent
                qty = new_qty
                entry_fill = fill
                entry_cost_basis = spent
                trades.append({"timestamp": ts, "action": "BUY", "fill_price": fill, "qty": qty, "realized_pnl": 0.0})

        elif pending_target == 0 and qty > 0:
            fill = o * (1 - sell_cost_rate)
            proceeds = qty * fill
            pnl = proceeds - entry_cost_basis
            cash += proceeds
            trades.append({"timestamp": ts, "action": "SELL", "fill_price": fill, "qty": qty, "realized_pnl": pnl})
            qty, entry_fill, entry_cost_basis = 0.0, np.nan, 0.0

        # Intrabar protective exits after open execution.
        if qty > 0:
            stop = entry_fill * (1 - risk.stop_loss_pct)
            target = entry_fill * (1 + risk.take_profit_pct)
            exit_price = None
            reason = None
            # Conservative tie-break: if both touched in the same bar, assume stop first.
            if l <= stop:
                exit_price, reason = stop * (1 - sell_cost_rate), "STOP"
            elif h >= target:
                exit_price, reason = target * (1 - sell_cost_rate), "TAKE_PROFIT"
            if exit_price is not None:
                proceeds = qty * exit_price
                pnl = proceeds - entry_cost_basis
                cash += proceeds
                trades.append({"timestamp": ts, "action": "SELL", "fill_price": exit_price, "qty": qty,
                               "realized_pnl": pnl, "reason": reason})
                qty, entry_fill, entry_cost_basis = 0.0, np.nan, 0.0

        equity = cash + qty * c
        equity_vals.append(equity)
        equity_idx.append(ts)
        pending_target = int(sig.iloc[i] > 0)

    # Liquidate at final close with costs to avoid hiding open risk.
    if qty > 0:
        ts = data.index[-1]
        fill = float(data["close"].iloc[-1]) * (1 - sell_cost_rate)
        proceeds = qty * fill
        pnl = proceeds - entry_cost_basis
        cash += proceeds
        trades.append({"timestamp": ts, "action": "SELL", "fill_price": fill, "qty": qty,
                       "realized_pnl": pnl, "reason": "FINAL_LIQUIDATION"})
        equity_vals[-1] = cash

    equity = pd.Series(equity_vals, index=equity_idx, name="equity")
    trade_df = pd.DataFrame(trades)
    metrics = performance_metrics(equity, trade_df, bars_per_year, risk_free_rate)
    return {"equity": equity, "trades": trade_df, "metrics": metrics}
