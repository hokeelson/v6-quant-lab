from __future__ import annotations
import numpy as np
import pandas as pd
from .research import parameter_grid, strategy_signal, robustness_score
from .backtest import run_backtest, ExecutionCosts, RiskRules

def cross_asset_grid_search(datasets: dict[str, pd.DataFrame], strategy_name: str,
                            initial_capital: float, costs: ExecutionCosts,
                            risk: RiskRules, bars_per_year: int,
                            risk_free_rate: float = 0.0,
                            min_trades: int = 20,
                            oos_fraction: float = 0.30) -> pd.DataFrame:
    """
    Evaluate every parameter set on the untouched tail of every asset.
    This deliberately favors parameters that generalize across assets,
    not those with one spectacular single-symbol backtest.
    """
    if not datasets:
        return pd.DataFrame()

    rows = []
    for params in parameter_grid(strategy_name):
        asset_metrics = []
        for symbol, df in datasets.items():
            if df is None or len(df) < 120:
                continue
            cut = max(60, int(len(df) * (1 - oos_fraction)))
            test = df.iloc[cut:].copy()
            if len(test) < 40:
                continue
            sig = strategy_signal(strategy_name, test, params)
            result = run_backtest(
                test, sig, initial_capital, costs, risk,
                bars_per_year, risk_free_rate
            )
            m = result["metrics"]
            if not m:
                continue
            asset_metrics.append({
                "symbol": symbol,
                **m,
                "asset_score": robustness_score(m, min_trades)
            })

        if not asset_metrics:
            continue

        a = pd.DataFrame(asset_metrics)
        sh = a["sharpe"].replace([np.inf,-np.inf], np.nan)
        ret = a["total_return"].replace([np.inf,-np.inf], np.nan)
        mdd = a["max_drawdown"].replace([np.inf,-np.inf], np.nan)
        scores = a["asset_score"]

        positive_ratio = float((ret > 0).mean())
        sharpe_positive_ratio = float((sh > 0).mean())
        median_sharpe = float(sh.median()) if sh.notna().any() else np.nan
        median_return = float(ret.median()) if ret.notna().any() else np.nan
        median_mdd = float(mdd.median()) if mdd.notna().any() else np.nan
        worst_mdd = float(mdd.min()) if mdd.notna().any() else np.nan
        score_dispersion = float(scores.std(ddof=0)) if len(scores) else np.nan

        # Generalization score: cross-asset consistency dominates.
        gen_score = (
            0.35 * scores.mean()
            + 25.0 * positive_ratio
            + 15.0 * sharpe_positive_ratio
            - 0.10 * min(score_dispersion if np.isfinite(score_dispersion) else 50, 50)
        )
        gen_score = float(np.clip(gen_score, 0, 100))

        rows.append({
            "strategy": strategy_name,
            "params": params,
            "assets_tested": int(len(a)),
            "positive_asset_ratio": positive_ratio,
            "positive_sharpe_ratio": sharpe_positive_ratio,
            "median_oos_return": median_return,
            "median_oos_sharpe": median_sharpe,
            "median_oos_max_drawdown": median_mdd,
            "worst_oos_max_drawdown": worst_mdd,
            "mean_asset_score": float(scores.mean()),
            "score_dispersion": score_dispersion,
            "generalization_score": gen_score,
        })

    return pd.DataFrame(rows).sort_values(
        ["generalization_score","positive_asset_ratio","median_oos_sharpe"],
        ascending=False
    ).reset_index(drop=True)
