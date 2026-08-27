from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def session_label(ts) -> str:
    """Coarse UTC liquidity session label for research only."""
    hour = int(_utc(ts).hour)
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 13:
        return "EUROPE"
    if 13 <= hour < 17:
        return "EU_US_OVERLAP"
    return "US"


def _through(df: pd.DataFrame, cutoff) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    idx = pd.to_datetime(df.index, utc=True, errors="coerce")
    valid = ~pd.isna(idx)
    if not bool(np.any(valid)):
        return pd.DataFrame()
    positions = np.flatnonzero(np.asarray(valid, dtype=bool))
    valid_idx = pd.DatetimeIndex(idx[positions])
    mask = np.asarray(valid_idx.asi8 <= _utc(cutoff).value, dtype=bool)
    return df.iloc[positions[np.flatnonzero(mask)]].copy()


def market_context(ts, horizon: str, frames: Mapping[tuple[str, str], pd.DataFrame], btc_1h: pd.DataFrame) -> dict:
    """Cross-sectional research context from the existing OHLCV cache only.

    No external API is called. Values are descriptive telemetry and never feed
    the V2 router or position sizing.
    """
    cutoff = _utc(ts)
    btc = _through(btc_1h, cutoff)
    btc_ret_24h = None
    if len(btc) >= 25:
        prev = float(btc.close.iloc[-25])
        btc_ret_24h = float(btc.close.iloc[-1] / prev - 1.0) if prev else None

    above_ema20 = []
    one_bar_returns = []
    return_series = []
    symbols_used = []

    for (symbol, h), df in frames.items():
        if h != horizon:
            continue
        hist = _through(df, cutoff)
        if len(hist) < 3:
            continue
        close = pd.to_numeric(hist.close, errors="coerce").dropna()
        if len(close) < 3:
            continue
        symbols_used.append(str(symbol))
        one_bar_returns.append(float(close.iloc[-1] / close.iloc[-2] - 1.0))
        if len(close) >= 20:
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            above_ema20.append(float(close.iloc[-1]) > ema20)
        rets = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna().tail(24)
        if len(rets) >= 6:
            rets.name = str(symbol)
            return_series.append(rets)

    avg_corr = None
    if len(return_series) >= 3:
        aligned = pd.concat(return_series[:40], axis=1, join="inner").dropna(how="any")
        if len(aligned) >= 6 and aligned.shape[1] >= 3:
            corr = aligned.corr().to_numpy(dtype=float)
            tri = corr[np.triu_indices_from(corr, k=1)]
            finite = tri[np.isfinite(tri)]
            if finite.size:
                avg_corr = float(np.mean(finite))

    return {
        "version": 1,
        "session": session_label(ts),
        "horizon": str(horizon),
        "breadth_above_ema20": (float(np.mean(above_ema20)) if above_ema20 else None),
        "median_one_bar_return": (float(np.median(one_bar_returns)) if one_bar_returns else None),
        "avg_pairwise_correlation": avg_corr,
        "btc_return_24h": btc_ret_24h,
        "symbols_observed": len(symbols_used),
        "funding_rate": None,
        "open_interest_change": None,
        "liquidation_shock_score": None,
        "external_derivatives_data": "NOT_CONNECTED",
    }


def summarize_trades(trades: list[dict]) -> dict:
    tracked = [t for t in trades if t.get("mfe_pct") is not None and t.get("mae_pct") is not None]
    by_session: dict[str, dict] = {}
    for t in tracked:
        session = str(t.get("entry_session") or "UNKNOWN")
        bucket = by_session.setdefault(session, {"closed_trades": 0, "wins": 0, "realized_pnl": 0.0, "return_sum": 0.0})
        bucket["closed_trades"] += 1
        pnl = float(t.get("realized_pnl") or 0.0)
        ret = float(t.get("return_pct") or 0.0)
        bucket["wins"] += int(pnl > 0)
        bucket["realized_pnl"] += pnl
        bucket["return_sum"] += ret
    for bucket in by_session.values():
        n = int(bucket["closed_trades"])
        bucket["win_rate"] = bucket["wins"] / n if n else None
        bucket["avg_return_pct"] = bucket.pop("return_sum") / n if n else None

    return {
        "tracked_closed_trades": len(tracked),
        "avg_mfe_pct": (float(np.mean([float(t["mfe_pct"]) for t in tracked])) if tracked else None),
        "avg_mae_pct": (float(np.mean([float(t["mae_pct"]) for t in tracked])) if tracked else None),
        "by_session": by_session,
    }
