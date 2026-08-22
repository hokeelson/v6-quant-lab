import numpy as np
import pandas as pd
from src.scanner import coarse_strategy_scan, select_finalists, ScanThresholds
from src.backtest import ExecutionCosts, RiskRules

def mk_df(seed=1, n=500):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    ret = rng.normal(0.0008, 0.015, n)
    close = 100*np.cumprod(1+ret)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close)*(1+rng.uniform(0,0.01,n))
    low = np.minimum(open_, close)*(1-rng.uniform(0,0.01,n))
    return pd.DataFrame({
        "open":open_,"high":high,"low":low,"close":close,
        "volume":rng.integers(100000,1000000,n)
    }, index=idx)

def test_coarse_scan_returns_ranked_rows():
    data = {"AAA":mk_df(1), "BBB":mk_df(2)}
    th = ScanThresholds(min_bars=300, min_closed_trades=1, min_coarse_score=0, finalist_count=2)
    out = coarse_strategy_scan(
        data, 100000, ExecutionCosts(), RiskRules(),
        365, th, strategies=["Momentum"]
    )
    assert len(out) == 2
    assert "coarse_score" in out.columns
    assert out["coarse_score"].iloc[0] >= out["coarse_score"].iloc[-1]

def test_finalists_are_symbol_diversified():
    coarse = pd.DataFrame([
        {"symbol":"A","strategy":"Momentum","coarse_score":90,"passes_coarse":True},
        {"symbol":"A","strategy":"Trend MA","coarse_score":80,"passes_coarse":True},
        {"symbol":"B","strategy":"Momentum","coarse_score":70,"passes_coarse":True},
    ])
    out = select_finalists(coarse, ScanThresholds(finalist_count=5))
    assert list(out["symbol"]) == ["A","B"]
