import numpy as np
import pandas as pd

from src.advanced_stats import (
    probabilistic_sharpe_ratio,
    cscv_pbo,
    stationary_block_bootstrap,
)
from src.portfolio import inverse_vol_weights, portfolio_backtest
from src.regime import classify_regime

def test_psr_is_probability():
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.001, 0.01, 500))
    p = probabilistic_sharpe_ratio(r, benchmark_sharpe=0.0, bars_per_year=252)
    assert 0 <= p <= 1

def test_cscv_detects_dimensions():
    rng = np.random.default_rng(2)
    mat = pd.DataFrame({
        "a": rng.normal(0.0010, 0.01, 240),
        "b": rng.normal(0.0003, 0.01, 240),
        "c": rng.normal(-0.0002, 0.01, 240),
        "d": rng.normal(0.0000, 0.01, 240),
    })
    out = cscv_pbo(mat, partitions=6)
    assert out["splits"] > 0
    assert 0 <= out["pbo"] <= 1

def test_stationary_bootstrap_shape():
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.0005, 0.01, 300))
    out = stationary_block_bootstrap(r, n_paths=100, mean_block=10, horizon=200, seed=7)
    assert out.shape == (100, 2)
    assert out["max_drawdown"].le(0).all()

def test_inverse_vol_weights_use_prior_data():
    idx = pd.date_range("2020-01-01", periods=100)
    r = pd.DataFrame({
        "a": np.r_[np.repeat(0.01, 50), np.repeat(0.20, 50)],
        "b": np.r_[np.repeat(0.02, 100)],
    }, index=idx)
    w = inverse_vol_weights(r, lookback=20)
    # A large change on today's return cannot affect today's already-shifted weight.
    r2 = r.copy()
    r2.iloc[80, 0] = 10.0
    w2 = inverse_vol_weights(r2, lookback=20)
    assert np.allclose(w.iloc[80].values, w2.iloc[80].values, equal_nan=True)

def test_regime_is_same_length():
    idx = pd.date_range("2020-01-01", periods=300)
    close = np.linspace(100, 160, 300)
    df = pd.DataFrame({
        "open": close, "high": close*1.01, "low": close*.99,
        "close": close, "volume": 1000
    }, index=idx)
    out = classify_regime(df)
    assert len(out) == len(df)
    assert "regime" in out.columns
