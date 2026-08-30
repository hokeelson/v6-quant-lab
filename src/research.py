from __future__ import annotations
import itertools
import numpy as np
import pandas as pd
from .backtest import run_backtest, ExecutionCosts, RiskRules
from .strategies import trend_signal, momentum_signal, mean_reversion_signal, breakout_signal

def strategy_signal(name: str, df: pd.DataFrame, params: dict) -> pd.Series:
    if name == "Trend MA":
        return trend_signal(df, **params)
    if name == "Momentum":
        return momentum_signal(df, **params)
    if name == "Mean Reversion RSI":
        return mean_reversion_signal(df, **params)
    if name == "Breakout":
        return breakout_signal(df, **params)
    raise KeyError(name)

def parameter_grid(name: str):
    grids = {
        "Trend MA": {"fast":[10,20,30,50],"slow":[60,100,150,200]},
        "Momentum": {"lookback":[20,40,60,90,120],"threshold":[0.0,0.03,0.06,0.10]},
        "Mean Reversion RSI": {"rsi_n":[7,10,14,21],"entry":[20,25,30,35],"exit_":[50,55,60,65]},
        "Breakout": {"lookback":[20,40,55,80,120],"exit_lookback":[10,20,30,40]},
    }
    g = grids[name]
    keys = list(g)
    for values in itertools.product(*[g[k] for k in keys]):
        p = dict(zip(keys, values))
        if name == "Trend MA" and p["fast"] >= p["slow"]:
            continue
        if name == "Breakout" and p["exit_lookback"] >= p["lookback"]:
            continue
        yield p

def robustness_score(metrics: dict, min_trades: int = 20) -> float:
    """Bounded 0-100 research ranking with explicit thin-sample plausibility control."""
    if not metrics:
        return 0.0
    sharpe = float(np.nan_to_num(metrics.get("sharpe", np.nan), nan=-2.0, posinf=3.0, neginf=-3.0))
    sortino = float(np.nan_to_num(metrics.get("sortino", np.nan), nan=-2.0, posinf=4.0, neginf=-4.0))
    calmar = float(np.nan_to_num(metrics.get("calmar", np.nan), nan=-2.0, posinf=4.0, neginf=-4.0))
    mdd = abs(float(np.nan_to_num(metrics.get("max_drawdown", np.nan), nan=1.0)))
    trades = max(0, int(metrics.get("closed_trades", 0) or 0))
    pf_raw = metrics.get("profit_factor", 0.0)
    try:
        pf = float(pf_raw)
        if not np.isfinite(pf):
            pf = 4.0
    except Exception:
        pf = 0.0

    # Extreme Sharpe/PF saturate quickly; a PF of 16 is not treated as eight
    # times stronger evidence than PF 2. Thin OOS samples are explicitly shrunk.
    quality = (
        0.28*np.tanh(sharpe/2.0)
        + 0.16*np.tanh(sortino/3.0)
        + 0.16*np.tanh(calmar/3.0)
        + 0.10*np.tanh((min(max(pf, 0.0), 4.0)-1.0)/1.5)
    )
    risk = 0.15*(1 - min(mdd, 1.0))
    sample_ratio = min(trades/max(min_trades,1), 1.0)
    sample = 0.15*sample_ratio
    raw = float(np.clip((quality + risk + sample + 0.35) / 1.35 * 100, 0, 100))

    # Plausibility gate: very small samples cannot receive a near-perfect score
    # merely from lucky extreme risk metrics.
    if trades < 5:
        ceiling = 52.0
    elif trades < max(8, min_trades//2):
        ceiling = 62.0 + 8.0*sample_ratio
    elif trades < min_trades:
        ceiling = 72.0 + 13.0*sample_ratio
    else:
        ceiling = 100.0
    return float(min(raw, ceiling))

def grid_search(df: pd.DataFrame, strategy_name: str, initial_capital: float,
                costs: ExecutionCosts, risk: RiskRules, bars_per_year: int,
                risk_free_rate: float = 0.0, min_trades: int = 20) -> pd.DataFrame:
    rows = []
    for params in parameter_grid(strategy_name):
        sig = strategy_signal(strategy_name, df, params)
        result = run_backtest(df, sig, initial_capital, costs, risk, bars_per_year, risk_free_rate)
        m = result["metrics"]
        rows.append({"strategy":strategy_name, "params":params, **m,
                     "score":robustness_score(m, min_trades)})
    return pd.DataFrame(rows).sort_values(["score","sharpe"], ascending=False).reset_index(drop=True)

def walk_forward(df: pd.DataFrame, strategy_name: str, initial_capital: float,
                 costs: ExecutionCosts, risk: RiskRules, bars_per_year: int,
                 train_bars: int, test_bars: int, step_bars: int,
                 risk_free_rate: float = 0.0, min_trades: int = 20) -> pd.DataFrame:
    rows = []
    start = 0
    fold = 1
    while start + train_bars + test_bars <= len(df):
        train = df.iloc[start:start+train_bars]
        test = df.iloc[start+train_bars:start+train_bars+test_bars]
        ranking = grid_search(train, strategy_name, initial_capital, costs, risk,
                              bars_per_year, risk_free_rate, min_trades)
        if ranking.empty:
            break
        best_params = ranking.iloc[0]["params"]
        sig = strategy_signal(strategy_name, test, best_params)
        oos = run_backtest(test, sig, initial_capital, costs, risk, bars_per_year, risk_free_rate)
        m = oos["metrics"]
        rows.append({"fold":fold,"train_start":train.index[0],"train_end":train.index[-1],
                     "test_start":test.index[0],"test_end":test.index[-1],"params":best_params,
                     **m,"oos_score":robustness_score(m,min_trades)})
        fold += 1
        start += step_bars
    return pd.DataFrame(rows)

def stress_test(df: pd.DataFrame, strategy_name: str, params: dict, initial_capital: float,
                base_costs: ExecutionCosts, base_risk: RiskRules, bars_per_year: int,
                risk_free_rate: float = 0.0) -> pd.DataFrame:
    scenarios = [("Base",1.0,0.0),("Costs x2",2.0,0.0),("Costs x3",3.0,0.0),("Entry +10bps",1.0,10.0),("Entry +25bps",1.0,25.0)]
    rows = []
    sig = strategy_signal(strategy_name, df, params)
    for name, mult, extra_slip in scenarios:
        c = ExecutionCosts(commission_bps=base_costs.commission_bps*mult,
                           slippage_bps=base_costs.slippage_bps*mult+extra_slip,
                           spread_bps=base_costs.spread_bps*mult)
        r = run_backtest(df, sig, initial_capital, c, base_risk, bars_per_year, risk_free_rate)
        rows.append({"scenario":name, **r["metrics"]})
    return pd.DataFrame(rows)
