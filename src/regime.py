from __future__ import annotations
import numpy as np
import pandas as pd

def classify_regime(df: pd.DataFrame, trend_window: int = 100, vol_window: int = 20) -> pd.DataFrame:
    """
    Transparent rule-based market regime model.
    No future data: all features at t use data at or before t.
    Regimes: Bull/LowVol, Bull/HighVol, Bear/LowVol, Bear/HighVol, Sideways.
    """
    x = df.copy()
    ret = x["close"].pct_change()
    ma = x["close"].rolling(trend_window, min_periods=trend_window).mean()
    slope = ma.pct_change(20)
    rv = ret.rolling(vol_window, min_periods=vol_window).std()
    vol_med = rv.rolling(max(120, vol_window*6), min_periods=max(60, vol_window*3)).median()

    trend_up = (x["close"] > ma) & (slope > 0)
    trend_down = (x["close"] < ma) & (slope < 0)
    high_vol = rv > vol_med

    regime = pd.Series("Sideways", index=x.index, dtype=object)
    regime[trend_up & ~high_vol] = "Bull/LowVol"
    regime[trend_up & high_vol] = "Bull/HighVol"
    regime[trend_down & ~high_vol] = "Bear/LowVol"
    regime[trend_down & high_vol] = "Bear/HighVol"

    return pd.DataFrame({
        "return": ret,
        "trend_ma": ma,
        "trend_slope_20": slope,
        "realized_vol": rv,
        "vol_median": vol_med,
        "regime": regime,
    }, index=x.index)

def performance_by_regime(strategy_returns: pd.Series, regimes: pd.Series, bars_per_year: int) -> pd.DataFrame:
    x = pd.DataFrame({"r": strategy_returns, "regime": regimes}).dropna()
    rows = []
    for name, g in x.groupby("regime"):
        r = g["r"]
        sd = r.std(ddof=1)
        sharpe = r.mean()/sd*np.sqrt(bars_per_year) if len(r)>1 and sd>0 else np.nan
        rows.append({
            "regime": name,
            "bars": int(len(r)),
            "mean_bar_return": float(r.mean()),
            "compound_return": float((1+r).prod()-1),
            "sharpe": float(sharpe) if np.isfinite(sharpe) else np.nan,
            "positive_bar_ratio": float((r>0).mean()),
        })
    return pd.DataFrame(rows).sort_values("bars", ascending=False)
