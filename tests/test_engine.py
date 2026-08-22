import numpy as np
import pandas as pd
from src.backtest import run_backtest, ExecutionCosts, RiskRules
from src.metrics import max_drawdown

def sample_df(n=300):
    idx = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    close = 100*np.exp(np.linspace(0,0.5,n))
    return pd.DataFrame({
        "open":close, "high":close*1.01, "low":close*0.99, "close":close, "volume":1_000_000
    }, index=idx)

def test_no_same_bar_execution():
    df = sample_df(20)
    sig = pd.Series(0.0, index=df.index)
    sig.iloc[5:] = 1.0
    r = run_backtest(df, sig, 100000, ExecutionCosts(), RiskRules(), 365)
    first_buy = r["trades"][r["trades"]["action"]=="BUY"].iloc[0]
    assert first_buy["timestamp"] == df.index[6]

def test_costs_reduce_equity():
    df = sample_df()
    sig = pd.Series(1.0, index=df.index)
    a = run_backtest(df, sig, 100000, ExecutionCosts(), RiskRules(), 365)
    b = run_backtest(df, sig, 100000, ExecutionCosts(10,10,10), RiskRules(), 365)
    assert b["equity"].iloc[-1] < a["equity"].iloc[-1]

def test_drawdown():
    eq = pd.Series([100,120,90,110], dtype=float)
    assert abs(max_drawdown(eq) - (-0.25)) < 1e-12
