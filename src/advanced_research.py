from __future__ import annotations
import pandas as pd
import numpy as np
from .research import parameter_grid, strategy_signal
from .backtest import run_backtest, ExecutionCosts, RiskRules
from .advanced_stats import cscv_pbo, deflated_sharpe_ratio, stationary_block_bootstrap, bootstrap_summary
from .robustness import parameter_neighborhood_stability
from .regime import classify_regime, performance_by_regime

def trial_return_matrix(df, strategy_name, initial_capital, costs, risk, bars_per_year, rf_annual=0.0):
    curves = {}
    sharpes = {}
    meta = {}
    for i, params in enumerate(parameter_grid(strategy_name)):
        sig = strategy_signal(strategy_name, df, params)
        bt = run_backtest(df, sig, initial_capital, costs, risk, bars_per_year, rf_annual)
        eq = bt["equity"]
        key = f"trial_{i:04d}"
        curves[key] = eq.pct_change()
        sharpes[key] = bt["metrics"].get("sharpe", np.nan)
        meta[key] = params
    mat = pd.DataFrame(curves).replace([np.inf,-np.inf], np.nan)
    return mat, pd.Series(sharpes), meta

def advanced_validation(df, strategy_name, ranking, initial_capital,
                        costs: ExecutionCosts, risk: RiskRules, bars_per_year,
                        rf_annual=0.0, pbo_partitions=8, bootstrap_paths=2000):
    if ranking is None or ranking.empty:
        raise ValueError("Run grid search first.")
    best_params = ranking.iloc[0]["params"]
    sig = strategy_signal(strategy_name, df, best_params)
    best = run_backtest(df, sig, initial_capital, costs, risk, bars_per_year, rf_annual)
    best_returns = best["equity"].pct_change().dropna()

    matrix, trial_sharpes, meta = trial_return_matrix(
        df, strategy_name, initial_capital, costs, risk, bars_per_year, rf_annual
    )
    pbo = cscv_pbo(matrix, partitions=pbo_partitions)
    dsr = deflated_sharpe_ratio(best_returns, trial_sharpes, bars_per_year, rf_annual)
    boot = stationary_block_bootstrap(best_returns, n_paths=bootstrap_paths, mean_block=10)
    boot_summary = bootstrap_summary(boot)
    stability = parameter_neighborhood_stability(ranking)

    regimes = classify_regime(df)
    aligned_strategy_returns = best["equity"].pct_change().reindex(df.index)
    regime_perf = performance_by_regime(aligned_strategy_returns, regimes["regime"], bars_per_year)

    return {
        "best_params": best_params,
        "best_backtest": best,
        "pbo": pbo,
        "dsr": dsr,
        "bootstrap": boot,
        "bootstrap_summary": boot_summary,
        "stability": stability,
        "regime_table": regime_perf,
    }
