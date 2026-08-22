from __future__ import annotations
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Callable
import numpy as np
import pandas as pd

from .data import AlpacaData, BinanceData, validate_ohlcv
from .backtest import ExecutionCosts, RiskRules, run_backtest
from .research import strategy_signal, grid_search, walk_forward, stress_test
from .advanced_research import advanced_validation
from .robustness import parameter_neighborhood_stability, final_research_grade

STRATEGIES = ["Trend MA", "Momentum", "Mean Reversion RSI", "Breakout"]

COARSE_PARAMS = {
    "Trend MA": [
        {"fast":10,"slow":60},
        {"fast":20,"slow":100},
        {"fast":50,"slow":200},
    ],
    "Momentum": [
        {"lookback":20,"threshold":0.00},
        {"lookback":60,"threshold":0.03},
        {"lookback":120,"threshold":0.06},
    ],
    "Mean Reversion RSI": [
        {"rsi_n":7,"entry":25,"exit_":55},
        {"rsi_n":14,"entry":30,"exit_":55},
        {"rsi_n":21,"entry":35,"exit_":60},
    ],
    "Breakout": [
        {"lookback":20,"exit_lookback":10},
        {"lookback":55,"exit_lookback":20},
        {"lookback":120,"exit_lookback":40},
    ],
}

@dataclass
class ScanThresholds:
    min_bars: int = 300
    min_price: float = 1.0
    min_closed_trades: int = 5
    max_drawdown_abs: float = 0.60
    min_coarse_score: float = 35.0
    finalist_count: int = 12

def discover_stock_universe(
    alpaca: AlpacaData,
    max_assets_to_snapshot: int = 1500,
    max_symbols: int = 300,
    min_price: float = 1.0,
    min_dollar_volume_proxy: float = 500_000,
    feed: str = "iex",
) -> pd.DataFrame:
    """
    Dynamic US-equity discovery:
    current active+tradable Alpaca assets -> snapshots -> rank by feed-specific
    daily dollar-volume proxy.

    Important: with feed=iex this is an IEX liquidity proxy, not total SIP volume.
    """
    assets = alpaca.assets("active", "us_equity")
    if assets.empty:
        return pd.DataFrame()
    x = assets.copy()
    for col in ["tradable","fractionable"]:
        if col in x:
            x[col] = x[col].fillna(False).astype(bool)
    x = x[x.get("tradable", False) == True]
    if "exchange" in x:
        x = x[x["exchange"].isin(["NASDAQ","NYSE","ARCA","AMEX","BATS","NYSEARCA"])]
    # Build a broad but rate-limit-aware candidate sample. Always include Alpaca's
    # current most-active names, then fill the remaining capacity with a
    # deterministic hash sample from the active tradable universe. This avoids
    # alphabetic/list-order selection bias when the user caps discovery size.
    try:
        active_now = alpaca.most_active(top=100, by="volume")
        active_syms = set(active_now["symbol"].astype(str)) if "symbol" in active_now else set()
    except Exception:
        active_syms = set()

    cap = max(int(max_assets_to_snapshot), 100)
    x["_priority"] = x["symbol"].isin(active_syms).astype(int)
    x["_hash"] = x["symbol"].astype(str).map(
        lambda z: int.from_bytes(__import__("hashlib").sha256(z.encode()).digest()[:8], "big")
    )
    x = x.sort_values(["_priority","_hash"], ascending=[False, True]).head(cap)
    snaps = alpaca.snapshots(x["symbol"].tolist(), feed=feed, batch_size=100)
    if snaps.empty:
        return pd.DataFrame()
    meta_cols = [c for c in ["symbol","name","exchange","fractionable","shortable","easy_to_borrow"] if c in x.columns]
    out = snaps.merge(x[meta_cols], on="symbol", how="left")
    out = out[
        (out["price"] >= float(min_price))
        & (out["dollar_volume_proxy"] >= float(min_dollar_volume_proxy))
    ]
    spread = (out["ask"] - out["bid"]) / ((out["ask"] + out["bid"]) / 2)
    out["spread_pct_proxy"] = spread.replace([np.inf,-np.inf], np.nan)
    return out.sort_values(
        ["dollar_volume_proxy","daily_volume"], ascending=False
    ).head(int(max_symbols)).reset_index(drop=True)

def discover_crypto_universe(
    binance: BinanceData,
    max_symbols: int = 200,
    min_quote_volume: float = 2_000_000,
    quote_asset: str = "USDT",
) -> pd.DataFrame:
    out = binance.discover_spot_universe(
        quote_asset=quote_asset,
        min_quote_volume=min_quote_volume,
        max_symbols=max_symbols,
    )
    if out.empty:
        return out
    return out.rename(columns={
        "lastPrice":"price",
        "quoteVolume":"dollar_volume_proxy",
        "count":"trades_24h",
    })

def coarse_strategy_scan(
    datasets: dict[str, pd.DataFrame],
    initial_capital: float,
    costs: ExecutionCosts,
    risk: RiskRules,
    bars_per_year: int,
    thresholds: ScanThresholds,
    rf_annual: float = 0.0,
    strategies: list[str] | None = None,
    progress: Callable[[int,int,str],None] | None = None,
) -> pd.DataFrame:
    """
    Stage-1/2 funnel. Uses a small fixed parameter set on a strict tail OOS window.
    It does NOT optimize parameters here; that reduces compute and selection bias.
    """
    strategies = strategies or STRATEGIES
    rows = []
    total = max(len(datasets) * len(strategies), 1)
    done = 0
    for symbol, df in datasets.items():
        v = validate_ohlcv(df)
        critical = sum(v[k] for k in [
            "duplicates","missing","bad_high","bad_low",
            "nonpositive_price","negative_volume","non_monotonic_time"
        ])
        if critical or len(df) < thresholds.min_bars:
            continue
        cut = max(int(len(df)*0.70), 100)
        test = df.iloc[cut:].copy()
        if len(test) < 80:
            continue

        for strategy in strategies:
            best = None
            for params in COARSE_PARAMS[strategy]:
                sig = strategy_signal(strategy, test, params)
                bt = run_backtest(
                    test, sig, initial_capital, costs, risk,
                    bars_per_year, rf_annual
                )
                m = bt["metrics"]
                dd = abs(float(m.get("max_drawdown", 1.0)))
                tr = int(m.get("closed_trades", 0))
                sh = float(np.nan_to_num(m.get("sharpe", np.nan), nan=-5.0))
                cal = float(np.nan_to_num(m.get("calmar", np.nan), nan=-5.0))
                ret = float(np.nan_to_num(m.get("total_return", np.nan), nan=-1.0))
                # Bounded 0-100 coarse score. Each component is normalized first
                # so one strong metric cannot saturate the whole ranking.
                sharpe_component = 50 + 50*np.tanh(sh/2)
                calmar_component = 50 + 50*np.tanh(cal/2)
                return_component = 50 + 50*np.tanh(ret*2)
                drawdown_component = 100*(1-min(dd,1))
                sample_component = 100*min(
                    tr/max(thresholds.min_closed_trades,1), 1
                )
                score = (
                    0.30*sharpe_component
                    + 0.15*calmar_component
                    + 0.15*return_component
                    + 0.20*drawdown_component
                    + 0.20*sample_component
                )
                score = float(np.clip(score, 0, 100))
                candidate = {
                    "symbol":symbol, "strategy":strategy, "params":params,
                    "coarse_score":score, **m,
                }
                if best is None or candidate["coarse_score"] > best["coarse_score"]:
                    best = candidate
            if best is not None:
                best["passes_coarse"] = bool(
                    best["coarse_score"] >= thresholds.min_coarse_score
                    and abs(best["max_drawdown"]) <= thresholds.max_drawdown_abs
                    and best["closed_trades"] >= thresholds.min_closed_trades
                )
                rows.append(best)
            done += 1
            if progress:
                progress(done, total, f"{symbol} / {strategy}")
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    return out.sort_values(
        ["passes_coarse","coarse_score","sharpe"],
        ascending=[False,False,False]
    ).reset_index(drop=True)

def select_finalists(coarse: pd.DataFrame, thresholds: ScanThresholds) -> pd.DataFrame:
    if coarse is None or coarse.empty:
        return pd.DataFrame()
    x = coarse[coarse["passes_coarse"] == True].copy()
    # Diversify: first keep best strategy per symbol, then global rank.
    x = x.sort_values("coarse_score", ascending=False).drop_duplicates("symbol")
    return x.head(int(thresholds.finalist_count)).reset_index(drop=True)

def deep_validate_finalists(
    datasets: dict[str, pd.DataFrame],
    finalists: pd.DataFrame,
    initial_capital: float,
    costs: ExecutionCosts,
    risk: RiskRules,
    bars_per_year: int,
    train_bars: int,
    test_bars: int,
    step_bars: int,
    rf_annual: float = 0.0,
    min_trades: int = 20,
    advanced_top_n: int = 5,
    bootstrap_paths: int = 1000,
    progress: Callable[[int,int,str],None] | None = None,
) -> pd.DataFrame:
    """
    Full grid + walk-forward + stress + neighborhood stability for all finalists.
    PBO/DSR/Bootstrap are limited to the highest-ranked finalists because they are
    much more expensive.
    """
    rows = []
    total = max(len(finalists), 1)
    preliminary = []

    for i, row in finalists.iterrows():
        symbol, strategy = row["symbol"], row["strategy"]
        df = datasets[symbol]
        ranking = grid_search(
            df, strategy, initial_capital, costs, risk, bars_per_year,
            rf_annual, min_trades
        )
        wf = walk_forward(
            df, strategy, initial_capital, costs, risk, bars_per_year,
            train_bars, test_bars, step_bars, rf_annual, min_trades
        )
        stress = None
        stability = {}
        if ranking is not None and len(ranking):
            stability = parameter_neighborhood_stability(ranking)
            stress = stress_test(
                df, strategy, ranking.iloc[0]["params"],
                initial_capital, costs, risk, bars_per_year, rf_annual
            )

        oos_score = float(wf["oos_score"].mean()) if wf is not None and len(wf) else np.nan
        oos_sharpe_med = float(wf["sharpe"].median()) if wf is not None and len(wf) else np.nan
        oos_positive = float((wf["total_return"] > 0).mean()) if wf is not None and len(wf) else np.nan
        stress_survival = np.nan
        if stress is not None and len(stress) > 1:
            base = stress.iloc[0]["sharpe"]
            s = stress["sharpe"].iloc[1:].replace([np.inf,-np.inf], np.nan).dropna()
            if pd.notna(base) and base > 0 and len(s):
                stress_survival = float(np.clip(s.median()/base*100,0,100))
            elif pd.notna(base):
                stress_survival = 0.0

        prelim = {
            "symbol":symbol, "strategy":strategy,
            "best_params": ranking.iloc[0]["params"] if ranking is not None and len(ranking) else {},
            "full_grid_score": float(ranking.iloc[0]["score"]) if ranking is not None and len(ranking) else np.nan,
            "full_grid_sharpe": float(ranking.iloc[0]["sharpe"]) if ranking is not None and len(ranking) else np.nan,
            "oos_score": oos_score,
            "oos_sharpe_median": oos_sharpe_med,
            "oos_positive_fold_ratio": oos_positive,
            "parameter_stability": float(stability.get("stability_score", np.nan)),
            "stress_survival": stress_survival,
            "_ranking": ranking,
            "_wf": wf,
        }
        preliminary.append(prelim)
        if progress:
            progress(i+1, total, f"Deep: {symbol} / {strategy}")

    # Decide which finalists deserve expensive advanced statistics.
    order = sorted(
        range(len(preliminary)),
        key=lambda j: (
            np.nan_to_num(preliminary[j]["oos_score"], nan=-999),
            np.nan_to_num(preliminary[j]["oos_sharpe_median"], nan=-999),
        ),
        reverse=True
    )
    advanced_set = set(order[:min(int(advanced_top_n), len(order))])

    for j, p in enumerate(preliminary):
        pbo = dsr = boot_loss = np.nan
        if j in advanced_set and p["_ranking"] is not None and len(p["_ranking"]):
            adv = advanced_validation(
                datasets[p["symbol"]], p["strategy"], p["_ranking"],
                initial_capital, costs, risk, bars_per_year, rf_annual,
                pbo_partitions=8, bootstrap_paths=bootstrap_paths
            )
            pbo = adv["pbo"].get("pbo", np.nan)
            dsr = adv["dsr"].get("deflated_sharpe_probability", np.nan)
            boot_loss = adv["bootstrap_summary"].get("probability_loss", np.nan)

        grade = final_research_grade(
            None if not np.isfinite(p["oos_score"]) else p["oos_score"],
            None,  # cross-asset strategy generalization is a separate study
            None if not np.isfinite(pbo) else pbo,
            None if not np.isfinite(dsr) else dsr,
            None if not np.isfinite(p["parameter_stability"]) else p["parameter_stability"],
            None if not np.isfinite(p["stress_survival"]) else p["stress_survival"],
            None if not np.isfinite(boot_loss) else boot_loss,
        )
        rows.append({
            k:v for k,v in p.items() if not k.startswith("_")
        } | {
            "pbo":pbo,
            "dsr_probability":dsr,
            "bootstrap_loss_probability":boot_loss,
            "research_grade":grade["grade"],
            "evidence_coverage":grade["evidence_coverage"],
            "advanced_stats_run": bool(j in advanced_set),
        })

    return pd.DataFrame(rows).sort_values(
        ["research_grade","oos_score","oos_sharpe_median"],
        ascending=False
    ).reset_index(drop=True)

def save_scan_checkpoint(
    directory: str | Path,
    universe: pd.DataFrame | None = None,
    coarse: pd.DataFrame | None = None,
    finalists: pd.DataFrame | None = None,
    deep: pd.DataFrame | None = None,
):
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    for name, df in [
        ("universe",universe),("coarse",coarse),
        ("finalists",finalists),("deep",deep)
    ]:
        if df is not None:
            out = df.copy()
            for c in out.columns:
                if out[c].map(lambda v: isinstance(v, dict)).any():
                    out[c] = out[c].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
            out.to_csv(d/f"{name}.csv", index=False, encoding="utf-8-sig")
