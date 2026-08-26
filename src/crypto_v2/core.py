from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MarketRegime:
    state: str
    trend: float
    realized_vol: float
    vol_ratio: float
    ret_fast: float
    ret_slow: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RoutedDecision:
    action: str
    strategy: str
    confidence: float
    stop_distance: float
    target_distance: float
    max_holding_bars: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _series(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) if b else 0.0


def classify_market_regime(btc_1h: pd.DataFrame) -> dict:
    """Classify the crypto environment from closed BTC 1h bars only.

    This deliberately uses a small transparent feature set. V2 is a forward shadow
    experiment; the classifier should remain interpretable before more complex
    funding/OI/liquidation features are introduced.
    """
    close = _series(btc_1h, "close")
    if len(close) < 96:
        return MarketRegime("INSUFFICIENT_DATA", 0.0, 0.0, 1.0, 0.0, 0.0, "Need >=96 BTC 1h bars").to_dict()

    returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    last = float(close.iloc[-1])
    ema24 = float(close.ewm(span=24, adjust=False).mean().iloc[-1])
    ema72 = float(close.ewm(span=72, adjust=False).mean().iloc[-1])
    ret_fast = _pct(last, float(close.iloc[-7]))
    ret_slow = _pct(last, float(close.iloc[-25]))
    trend = _pct(ema24, ema72)

    rv24 = float(returns.tail(24).std(ddof=0) * math.sqrt(24.0)) if len(returns) >= 24 else 0.0
    rolling = returns.rolling(24).std(ddof=0) * math.sqrt(24.0)
    ref = float(rolling.tail(24 * 14).median()) if rolling.notna().any() else rv24
    if not np.isfinite(ref) or ref <= 1e-9:
        ref = max(rv24, 1e-9)
    vol_ratio = rv24 / ref

    if ret_slow <= -0.10 or (ret_fast <= -0.06 and vol_ratio >= 1.6):
        state, reason = "PANIC", "BTC drawdown + volatility shock"
    elif vol_ratio >= 1.8 and abs(trend) < 0.025:
        state, reason = "HIGH_VOL_SIDEWAYS", "Volatility elevated without stable trend"
    elif trend >= 0.018 and ret_slow > 0:
        state, reason = "TREND_UP", "BTC fast EMA above slow EMA with positive 24h return"
    elif trend <= -0.018 and ret_slow < 0:
        state, reason = "TREND_DOWN", "BTC fast EMA below slow EMA with negative 24h return"
    elif vol_ratio <= 0.72:
        state, reason = "LOW_VOL_SIDEWAYS", "Volatility compression"
    else:
        state, reason = "SIDEWAYS", "No dominant directional regime"

    return MarketRegime(state, trend, rv24, vol_ratio, ret_fast, ret_slow, reason).to_dict()


def symbol_features(df: pd.DataFrame, btc_df: pd.DataFrame | None = None) -> dict:
    close = _series(df, "close")
    high = _series(df, "high")
    low = _series(df, "low")
    volume = _series(df, "volume")
    if len(close) < 40:
        return {"ready": False, "bars": int(len(close))}

    last = float(close.iloc[-1])
    ret_fast = _pct(last, float(close.iloc[-7]))
    ret_slow = _pct(last, float(close.iloc[-25]))
    mean20 = float(close.tail(20).mean())
    std20 = float(close.tail(20).std(ddof=0))
    zscore20 = (last - mean20) / std20 if std20 > 1e-12 else 0.0
    prior_high = float(high.iloc[-21:-1].max()) if len(high) >= 21 else float(high.max())
    breakout_pct = _pct(last, prior_high)

    v20 = volume.tail(20)
    vmean = float(v20.mean()) if len(v20) else 0.0
    vstd = float(v20.std(ddof=0)) if len(v20) else 0.0
    volume_z = (float(volume.iloc[-1]) - vmean) / vstd if vstd > 1e-12 else 0.0

    prev = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev).abs(),
        (low - prev).abs(),
    ], axis=1).max(axis=1).dropna()
    atr_pct = float(tr.tail(14).mean() / last) if last > 0 and not tr.empty else 0.02
    atr_pct = float(np.clip(atr_pct, 0.003, 0.20))

    relative_strength = 0.0
    if btc_df is not None and not btc_df.empty:
        btc_close = _series(btc_df, "close")
        if len(btc_close) >= 25:
            btc_ret = _pct(float(btc_close.iloc[-1]), float(btc_close.iloc[-25]))
            relative_strength = ret_slow - btc_ret

    returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    realized_vol = float(returns.tail(24).std(ddof=0) * math.sqrt(24.0)) if len(returns) >= 24 else 0.0

    return {
        "ready": True,
        "bars": int(len(close)),
        "price": last,
        "ret_fast": ret_fast,
        "ret_slow": ret_slow,
        "relative_strength": relative_strength,
        "zscore20": zscore20,
        "breakout_pct": breakout_pct,
        "volume_z": volume_z,
        "atr_pct": atr_pct,
        "realized_vol": realized_vol,
    }


def route_strategy(regime: dict, features: dict, horizon: str = "short") -> dict:
    if not features.get("ready"):
        return RoutedDecision("NO_TRADE", "NONE", 0.0, 0.0, 0.0, 0, "Insufficient symbol history").to_dict()

    state = str(regime.get("state") or "INSUFFICIENT_DATA")
    atr = float(features.get("atr_pct") or 0.02)
    ret_fast = float(features.get("ret_fast") or 0.0)
    ret_slow = float(features.get("ret_slow") or 0.0)
    rs = float(features.get("relative_strength") or 0.0)
    z = float(features.get("zscore20") or 0.0)
    volume_z = float(features.get("volume_z") or 0.0)
    breakout = float(features.get("breakout_pct") or 0.0)

    holding = {"short": 8, "medium": 10, "long": 12}.get(horizon, 8)

    # V2 starts long-only and explicitly treats unstable downside regimes as a
    # valid NO_TRADE outcome rather than forcing a strategy to stay active.
    if state in {"INSUFFICIENT_DATA", "PANIC", "TREND_DOWN", "HIGH_VOL_SIDEWAYS"}:
        return RoutedDecision("NO_TRADE", "NONE", 0.0, 0.0, 0.0, 0, f"Risk-off regime: {state}").to_dict()

    if state == "TREND_UP" and ret_fast > 0 and ret_slow > 0 and rs > -0.01 and volume_z > -0.75:
        score = 0.55 + min(0.25, max(0.0, ret_fast) * 4.0) + min(0.15, max(0.0, rs) * 3.0)
        return RoutedDecision(
            "ENTER", "V2_MOMENTUM", float(np.clip(score, 0.55, 0.90)),
            float(np.clip(2.2 * atr, 0.018, 0.12)),
            float(np.clip(4.0 * atr, 0.035, 0.24)), holding,
            "Trend-up + positive relative strength",
        ).to_dict()

    if state == "LOW_VOL_SIDEWAYS" and breakout > 0 and volume_z >= 1.0:
        score = 0.58 + min(0.20, volume_z * 0.04) + min(0.12, breakout * 6.0)
        return RoutedDecision(
            "ENTER", "V2_BREAKOUT", float(np.clip(score, 0.58, 0.88)),
            float(np.clip(1.8 * atr, 0.015, 0.09)),
            float(np.clip(4.2 * atr, 0.035, 0.22)), holding,
            "Volatility compression breakout with volume confirmation",
        ).to_dict()

    if state in {"SIDEWAYS", "LOW_VOL_SIDEWAYS"} and z <= -1.6 and ret_fast < 0 and volume_z < 2.5:
        score = 0.56 + min(0.22, abs(z + 1.6) * 0.08)
        return RoutedDecision(
            "ENTER", "V2_MEAN_REVERSION", float(np.clip(score, 0.56, 0.82)),
            float(np.clip(1.6 * atr, 0.012, 0.08)),
            float(np.clip(2.8 * atr, 0.025, 0.14)), max(4, holding - 2),
            "Sideways oversold mean-reversion setup",
        ).to_dict()

    return RoutedDecision("NO_TRADE", "NONE", 0.0, 0.0, 0.0, 0, f"No V2 setup in {state}").to_dict()
