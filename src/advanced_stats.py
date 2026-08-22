from __future__ import annotations
import itertools
import math
import numpy as np
import pandas as pd
from scipy.stats import norm, skew, kurtosis

def sharpe_ratio(returns: pd.Series, bars_per_year: int, rf_annual: float = 0.0) -> float:
    r = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 2:
        return np.nan
    rf_bar = (1 + rf_annual) ** (1 / bars_per_year) - 1
    x = r - rf_bar
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(bars_per_year)) if sd > 0 else np.nan

def probabilistic_sharpe_ratio(
    returns: pd.Series,
    benchmark_sharpe: float,
    bars_per_year: int,
    rf_annual: float = 0.0
) -> float:
    """
    Bailey & Lopez de Prado PSR-style statistic.
    Sharpe values are annualized externally but standardized using sample moments
    at the bar frequency to keep units internally consistent.
    """
    r = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna()
    n = len(r)
    if n < 3:
        return np.nan
    rf_bar = (1 + rf_annual) ** (1 / bars_per_year) - 1
    x = r - rf_bar
    sd = x.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return np.nan
    sr_bar = x.mean() / sd
    benchmark_bar = benchmark_sharpe / math.sqrt(bars_per_year)
    sk = float(skew(x, bias=False))
    ku = float(kurtosis(x, fisher=False, bias=False))
    denom_sq = 1 - sk * sr_bar + ((ku - 1) / 4.0) * (sr_bar ** 2)
    if denom_sq <= 0:
        return np.nan
    z = (sr_bar - benchmark_bar) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return float(norm.cdf(z))

def expected_max_sharpe(trial_sharpes: pd.Series) -> float:
    """
    Expected maximum Sharpe benchmark used by the 2014 DSR implementation.
    Uses observed dispersion across strategy trials and the Euler-Mascheroni
    interpolation between two extreme-value quantiles.
    """
    s = pd.Series(trial_sharpes).replace([np.inf, -np.inf], np.nan).dropna()
    n = len(s)
    if n <= 1:
        return float(s.iloc[0]) if n == 1 else np.nan
    sigma = float(s.std(ddof=1))
    mu = float(s.mean())
    if sigma <= 0:
        return mu
    gamma = 0.5772156649015329
    z1 = norm.ppf(1 - 1 / n)
    z2 = norm.ppf(1 - 1 / (n * math.e))
    return float(mu + sigma * ((1 - gamma) * z1 + gamma * z2))

def deflated_sharpe_ratio(
    selected_returns: pd.Series,
    trial_sharpes: pd.Series,
    bars_per_year: int,
    rf_annual: float = 0.0
) -> dict:
    benchmark = expected_max_sharpe(trial_sharpes)
    dsr = probabilistic_sharpe_ratio(
        selected_returns, benchmark, bars_per_year, rf_annual
    )
    selected_sr = sharpe_ratio(selected_returns, bars_per_year, rf_annual)
    return {
        "selected_sharpe": selected_sr,
        "expected_max_sharpe_null": benchmark,
        "deflated_sharpe_probability": dsr,
        "trials": int(pd.Series(trial_sharpes).replace([np.inf,-np.inf], np.nan).dropna().shape[0]),
    }

def cscv_pbo(returns_matrix: pd.DataFrame, partitions: int = 8) -> dict:
    """
    Combinatorially Symmetric Cross Validation (CSCV) PBO estimate.

    returns_matrix:
      rows = time observations, columns = alternative strategy trials.
    For each symmetric split, select the best IS strategy by Sharpe-like
    mean/std score, then evaluate its relative OOS rank.
    PBO = fraction of logit ranks < 0 (selected trial falls below median OOS).

    partitions must be even. Large values are combinatorial; 8 or 10 is practical.
    """
    x = returns_matrix.replace([np.inf, -np.inf], np.nan).dropna(how="all").copy()
    x = x.dropna(axis=1, how="all")
    if partitions < 4 or partitions % 2:
        raise ValueError("partitions must be even and >= 4")
    if x.shape[1] < 2 or x.shape[0] < partitions * 5:
        return {"pbo": np.nan, "splits": 0, "median_logit": np.nan, "oos_rank_percentiles": []}

    # Keep equal-length contiguous partitions; discard small remainder.
    usable = (len(x) // partitions) * partitions
    x = x.iloc[:usable]
    block_size = usable // partitions
    blocks = [np.arange(i*block_size, (i+1)*block_size) for i in range(partitions)]

    logits, rank_pcts = [], []
    half = partitions // 2

    # Complementary pairs are redundant for PBO estimate; process all unique IS choices.
    for is_blocks in itertools.combinations(range(partitions), half):
        is_set = set(is_blocks)
        oos_blocks = [i for i in range(partitions) if i not in is_set]
        is_idx = np.concatenate([blocks[i] for i in is_blocks])
        oos_idx = np.concatenate([blocks[i] for i in oos_blocks])

        ins = x.iloc[is_idx]
        outs = x.iloc[oos_idx]

        def score(df):
            mu = df.mean()
            sd = df.std(ddof=1).replace(0, np.nan)
            return mu / sd

        is_scores = score(ins)
        if is_scores.notna().sum() < 2:
            continue
        winner = is_scores.idxmax()

        oos_scores = score(outs)
        valid = oos_scores.dropna()
        if winner not in valid.index or len(valid) < 2:
            continue

        # pct rank in (0,1): 1 = best, near 0 = worst.
        ascending_rank = valid.rank(method="average", ascending=True)[winner]
        omega = float((ascending_rank - 0.5) / len(valid))
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        lam = math.log(omega / (1 - omega))
        logits.append(lam)
        rank_pcts.append(omega)

    if not logits:
        return {"pbo": np.nan, "splits": 0, "median_logit": np.nan, "oos_rank_percentiles": []}

    arr = np.asarray(logits, dtype=float)
    return {
        "pbo": float(np.mean(arr < 0)),
        "splits": int(len(arr)),
        "median_logit": float(np.median(arr)),
        "oos_rank_percentiles": [float(v) for v in rank_pcts],
    }

def stationary_block_bootstrap(
    returns: pd.Series,
    n_paths: int = 2000,
    mean_block: int = 10,
    horizon: int | None = None,
    seed: int = 42
) -> pd.DataFrame:
    """
    Stationary bootstrap-style resampling with geometrically distributed blocks.
    Preserves short-range serial dependence better than IID shuffling.
    """
    r = pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna().to_numpy(dtype=float)
    if len(r) < 20:
        return pd.DataFrame()
    horizon = int(horizon or len(r))
    mean_block = max(int(mean_block), 1)
    p_new = 1.0 / mean_block
    rng = np.random.default_rng(seed)
    paths = np.empty((n_paths, horizon), dtype=float)

    for j in range(n_paths):
        idx = rng.integers(0, len(r))
        for t in range(horizon):
            if t == 0 or rng.random() < p_new:
                idx = rng.integers(0, len(r))
            else:
                idx = (idx + 1) % len(r)
            paths[j, t] = r[idx]

    terminal = np.prod(1 + paths, axis=1) - 1
    wealth = np.cumprod(1 + paths, axis=1)
    peaks = np.maximum.accumulate(wealth, axis=1)
    dd = wealth / peaks - 1
    max_dd = dd.min(axis=1)
    return pd.DataFrame({
        "terminal_return": terminal,
        "max_drawdown": max_dd,
    })

def bootstrap_summary(boot: pd.DataFrame) -> dict:
    if boot is None or boot.empty:
        return {}
    return {
        "paths": int(len(boot)),
        "terminal_return_p05": float(boot["terminal_return"].quantile(.05)),
        "terminal_return_median": float(boot["terminal_return"].median()),
        "terminal_return_p95": float(boot["terminal_return"].quantile(.95)),
        "probability_loss": float((boot["terminal_return"] < 0).mean()),
        "max_drawdown_p05": float(boot["max_drawdown"].quantile(.05)),
        "max_drawdown_median": float(boot["max_drawdown"].median()),
    }
