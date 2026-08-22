from __future__ import annotations
import numpy as np
import pandas as pd

def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0)
    down = -d.clip(upper=0)
    gain = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    loss = down.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def trend_signal(df: pd.DataFrame, fast: int = 20, slow: int = 100) -> pd.Series:
    if fast >= slow:
        raise ValueError("fast must be < slow")
    f = df["close"].rolling(fast, min_periods=fast).mean()
    s = df["close"].rolling(slow, min_periods=slow).mean()
    return (f > s).astype(float).fillna(0.0)

def momentum_signal(df: pd.DataFrame, lookback: int = 60, threshold: float = 0.0) -> pd.Series:
    mom = df["close"].pct_change(lookback)
    return (mom > threshold).astype(float).fillna(0.0)

def mean_reversion_signal(df: pd.DataFrame, rsi_n: int = 14, entry: float = 30, exit_: float = 55) -> pd.Series:
    r = _rsi(df["close"], rsi_n)
    out = pd.Series(0.0, index=df.index)
    state = 0.0
    for i, value in enumerate(r):
        if np.isnan(value):
            out.iloc[i] = state
            continue
        if state == 0 and value < entry:
            state = 1.0
        elif state == 1 and value > exit_:
            state = 0.0
        out.iloc[i] = state
    return out

def breakout_signal(df: pd.DataFrame, lookback: int = 55, exit_lookback: int = 20) -> pd.Series:
    # Shift(1) is mandatory: today's decision only uses information known before today's close.
    prev_high = df["high"].rolling(lookback, min_periods=lookback).max().shift(1)
    prev_low_exit = df["low"].rolling(exit_lookback, min_periods=exit_lookback).min().shift(1)
    out = pd.Series(0.0, index=df.index)
    state = 0.0
    for i in range(len(df)):
        if pd.isna(prev_high.iloc[i]) or pd.isna(prev_low_exit.iloc[i]):
            out.iloc[i] = state
            continue
        if state == 0 and df["close"].iloc[i] > prev_high.iloc[i]:
            state = 1.0
        elif state == 1 and df["close"].iloc[i] < prev_low_exit.iloc[i]:
            state = 0.0
        out.iloc[i] = state
    return out
