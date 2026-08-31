from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .decision_engine import market_regime
from .external_intelligence import external_intelligence_assessment


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _sigmoid(value: float) -> float:
    value = _clamp(value, -8.0, 8.0)
    return 1.0 / (1.0 + math.exp(-value))


def _series(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[name], errors="coerce")


def _return(close: pd.Series, bars: int) -> float:
    if len(close) <= bars or close.iloc[-bars - 1] <= 0:
        return 0.0
    return _finite(close.iloc[-1] / close.iloc[-bars - 1] - 1.0)


def _bar_quality(df: pd.DataFrame) -> dict:
    """Local OHLCV integrity gate; upstream runtime health is checked separately."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {"score": 0.0, "critical": True, "reasons": ["EMPTY_MARKET_DATA"], "rows_checked": 0}
    recent = df.tail(120)
    required = ("open", "high", "low", "close")
    missing = [name for name in required if name not in recent]
    if missing:
        return {
            "score": 0.0,
            "critical": True,
            "reasons": ["MISSING_" + "_".join(name.upper() for name in missing)],
            "rows_checked": len(recent),
        }
    frame = pd.DataFrame({name: _series(recent, name) for name in required})
    finite = frame.replace([np.inf, -np.inf], np.nan).notna().all(axis=1)
    valid_ratio = float(finite.mean()) if len(frame) else 0.0
    valid = frame[finite]
    nonpositive_ratio = float((valid <= 0).any(axis=1).mean()) if len(valid) else 1.0
    invalid_range_ratio = float((valid["high"] < valid["low"]).mean()) if len(valid) else 1.0
    outside_ratio = float(((valid["close"] > valid["high"]) | (valid["close"] < valid["low"])).mean()) if len(valid) else 1.0
    duplicate_ratio = float(pd.Index(recent.index).duplicated().mean()) if len(recent) else 1.0
    negative_volume_ratio = 0.0
    if "volume" in recent:
        volume = _series(recent, "volume").replace([np.inf, -np.inf], np.nan)
        negative_volume_ratio = float((volume.dropna() < 0).mean()) if volume.notna().any() else 0.0
    reasons = []
    if valid_ratio < 0.98:
        reasons.append("NONFINITE_OHLC")
    if nonpositive_ratio > 0.0:
        reasons.append("NONPOSITIVE_OHLC")
    if invalid_range_ratio > 0.0:
        reasons.append("INVALID_HIGH_LOW_RANGE")
    if outside_ratio > 0.0:
        reasons.append("CLOSE_OUTSIDE_BAR")
    if duplicate_ratio > 0.0:
        reasons.append("DUPLICATE_TIMESTAMPS")
    if negative_volume_ratio > 0.0:
        reasons.append("NEGATIVE_VOLUME")
    penalty = (
        0.35 * (1.0 - valid_ratio)
        + 0.20 * nonpositive_ratio
        + 0.15 * invalid_range_ratio
        + 0.15 * outside_ratio
        + 0.10 * duplicate_ratio
        + 0.05 * negative_volume_ratio
    )
    critical = (
        valid_ratio < 0.95
        or nonpositive_ratio > 0.0
        or invalid_range_ratio > 0.0
        or outside_ratio > 0.02
        or duplicate_ratio > 0.0
        or negative_volume_ratio > 0.0
    )
    return {
        "score": _clamp(1.0 - penalty, 0.0, 1.0),
        "critical": critical,
        "reasons": reasons,
        "rows_checked": len(recent),
        "valid_ohlc_ratio": valid_ratio,
        "nonpositive_ratio": nonpositive_ratio,
        "invalid_range_ratio": invalid_range_ratio,
        "close_outside_ratio": outside_ratio,
        "duplicate_timestamp_ratio": duplicate_ratio,
        "negative_volume_ratio": negative_volume_ratio,
    }


def _trend_evidence(df: pd.DataFrame) -> dict:
    close = _series(df, "close").dropna()
    if len(close) < 70:
        return {"edge": 0.0, "quality": 0.0, "r5": 0.0, "r20": 0.0, "r60": 0.0}

    returns = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    volatility = max(_finite(returns.tail(60).std()), 0.002)
    r5, r20, r60 = (_return(close, bars) for bars in (5, 20, 60))
    normalized = (
        0.40 * r5 / (volatility * math.sqrt(5))
        + 0.35 * r20 / (volatility * math.sqrt(20))
        + 0.25 * r60 / (volatility * math.sqrt(60))
    )

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema60 = close.ewm(span=60, adjust=False).mean()
    ma_gap = _finite((ema20.iloc[-1] / ema60.iloc[-1] - 1.0) / (volatility * math.sqrt(20)))
    edge = _clamp(0.75 * normalized / 2.5 + 0.25 * ma_gap / 2.0, -1.0, 1.0)
    quality = _clamp(len(close) / 120.0, 0.0, 1.0)
    return {
        "edge": edge,
        "quality": quality,
        "r5": r5,
        "r20": r20,
        "r60": r60,
        "volatility": volatility,
        "ma_gap_normalized": ma_gap,
    }


def _volume_evidence(df: pd.DataFrame) -> dict:
    close = _series(df, "close")
    volume = _series(df, "volume")
    valid = pd.DataFrame({"close": close, "volume": volume}).replace([np.inf, -np.inf], np.nan).dropna()
    valid = valid[valid["volume"] >= 0]
    if len(valid) < 30 or valid["volume"].tail(30).sum() <= 0:
        return {
            "edge": 0.0,
            "quality": 0.0,
            "relative_volume": None,
            "pressure": 0.0,
            "obv_slope": 0.0,
            "breakout_confirmation": 0.0,
            "status": "UNAVAILABLE",
        }

    close = valid["close"]
    volume = valid["volume"]
    returns = close.pct_change().fillna(0.0)
    median_volume = _finite(volume.tail(20).median())
    relative_volume = _finite(volume.iloc[-1] / median_volume, 1.0) if median_volume > 0 else 1.0

    signed_flow = (returns * volume).tail(20)
    pressure = _finite(signed_flow.sum() / max(_finite(signed_flow.abs().sum()), 1e-12))

    obv = (np.sign(returns) * volume).cumsum()
    denominator = max(_finite(volume.tail(20).mean()) * 10.0, 1e-12)
    obv_slope = _clamp(_finite((obv.iloc[-1] - obv.iloc[-11]) / denominator), -1.0, 1.0)

    high20 = _finite(close.shift(1).rolling(20).max().iloc[-1], close.iloc[-1])
    low20 = _finite(close.shift(1).rolling(20).min().iloc[-1], close.iloc[-1])
    width = max(high20 - low20, abs(_finite(close.iloc[-1])) * 1e-6)
    location = _clamp(2.0 * (_finite(close.iloc[-1]) - low20) / width - 1.0, -1.0, 1.0)
    volume_confirmation = _clamp(math.log(max(relative_volume, 0.25)) / math.log(4.0), -1.0, 1.0)
    breakout_confirmation = location * max(0.0, volume_confirmation)

    edge = _clamp(0.50 * pressure + 0.30 * obv_slope + 0.20 * breakout_confirmation, -1.0, 1.0)
    nonzero_ratio = float((volume.tail(60) > 0).mean())
    freshness = _clamp(len(valid) / 80.0, 0.0, 1.0)
    plausibility = 1.0 if 0.05 <= relative_volume <= 20.0 else 0.5
    quality = _clamp(0.50 * nonzero_ratio + 0.30 * freshness + 0.20 * plausibility, 0.0, 1.0)
    return {
        "edge": edge,
        "quality": quality,
        "relative_volume": relative_volume,
        "pressure": pressure,
        "obv_slope": obv_slope,
        "breakout_confirmation": breakout_confirmation,
        "status": "AVAILABLE",
    }


def _regime_edge(regime: str) -> float:
    name = str(regime or "")
    if "UP_TREND" in name:
        return 0.75
    if "DOWN_TREND" in name:
        return -0.75
    return 0.0


def _technical_stability(df: pd.DataFrame, trend: dict) -> dict:
    close = _series(df, "close").dropna()
    if len(close) < 70:
        return {"score": 0.0, "timeframe_agreement": 0.0, "persistence": 0.0, "path_efficiency": 0.0, "volatility_quality": 0.0}

    directional = np.sign([trend.get("r5", 0.0), trend.get("r20", 0.0), trend.get("r60", 0.0)])
    nonzero = directional[directional != 0]
    timeframe_agreement = abs(float(nonzero.sum())) / len(nonzero) if len(nonzero) else 0.0

    rolling5 = close.pct_change(5).tail(12).dropna()
    direction = np.sign(_finite(trend.get("edge")))
    persistence = float((np.sign(rolling5) == direction).mean()) if direction and len(rolling5) else 0.0

    changes = close.diff().tail(20).abs()
    path_efficiency = _clamp(abs(_return(close, 20)) * _finite(close.iloc[-21]) / max(_finite(changes.sum()), 1e-12), 0.0, 1.0)

    returns = close.pct_change()
    current_vol = _finite(returns.tail(20).std())
    historical_vol = _finite(returns.rolling(20).std().tail(120).median())
    if current_vol > 0 and historical_vol > 0:
        volatility_quality = math.exp(-abs(math.log(current_vol / historical_vol)))
    else:
        volatility_quality = 0.5

    score = _clamp(
        0.30 * timeframe_agreement
        + 0.25 * persistence
        + 0.25 * path_efficiency
        + 0.20 * volatility_quality,
        0.0,
        1.0,
    )
    return {
        "score": score,
        "timeframe_agreement": timeframe_agreement,
        "persistence": persistence,
        "path_efficiency": path_efficiency,
        "volatility_quality": volatility_quality,
    }


def _forward_stability(performance_health: dict | None) -> dict:
    health = performance_health or {}
    available = bool(performance_health)
    samples = max(0, int(_finite(health.get("samples"), 0.0)))
    maturity = _clamp(samples / 20.0, 0.0, 1.0)
    multiplier = _clamp(_finite(health.get("shadow_weight_multiplier"), 1.0), 0.0, 1.0)
    calibration_stability = _clamp(_finite(health.get("model_stability"), 50.0) / 100.0, 0.0, 1.0)
    calibration_sample = _clamp(_finite(health.get("model_sample"), 0.0), 0.0, 1.0)
    prior_weight = 0.15 * calibration_sample
    forward_weight = 0.40 * maturity
    score = _clamp(
        0.50
        + prior_weight * (calibration_stability - 0.50)
        + forward_weight * (multiplier - 0.50),
        0.0,
        1.0,
    ) if available else 0.50
    return {
        "score": score,
        "maturity": maturity,
        "samples": samples,
        "state": str(health.get("state") or "LEARNING"),
        "forward_multiplier": multiplier,
        "calibration_stability": calibration_stability,
        "calibration_sample": calibration_sample,
        "available": available,
    }


def _adaptive_weights(regime: str, volume_quality: float, external_confidence: float) -> tuple[dict, float]:
    name = str(regime or "")
    if "HIGH_VOL" in name:
        base = {"trend": 0.28, "regime": 0.14, "volume": 0.30, "external": 0.28}
    elif "SIDEWAYS" in name:
        base = {"trend": 0.26, "regime": 0.12, "volume": 0.34, "external": 0.28}
    else:
        base = {"trend": 0.46, "regime": 0.20, "volume": 0.22, "external": 0.12}

    adjusted = dict(base)
    adjusted["volume"] *= _clamp(volume_quality, 0.0, 1.0)
    adjusted["external"] *= _clamp(external_confidence, 0.0, 1.0)
    if name == "UNKNOWN":
        adjusted["regime"] *= 0.20
    coverage = _clamp(sum(adjusted.values()) / sum(base.values()), 0.0, 1.0)
    total = sum(adjusted.values())
    if total <= 0:
        return {"trend": 1.0, "regime": 0.0, "volume": 0.0, "external": 0.0}, 0.0
    return {key: value / total for key, value in adjusted.items()}, coverage


def _preferred_playbook(direction: str, regime: str, volume: dict, event_risk: float) -> str:
    if direction == "NO_TRADE":
        return "WAIT"
    if event_risk >= 0.75:
        return "EVENT_RISK_DEFENSIVE"
    if "HIGH_VOL" in regime and abs(_finite(volume.get("edge"))) >= 0.20:
        return "CONFIRMED_BREAKOUT"
    if "SIDEWAYS" in regime:
        return "TACTICAL_MEAN_REVERSION"
    return "TREND_MOMENTUM"


def assess_direction(
    df: pd.DataFrame,
    market: str,
    strategy: str,
    stop_distance: float,
    target_distance: float,
    performance_health: dict | None = None,
) -> dict:
    """Adaptive LONG/SHORT/NO_TRADE Shadow assessment using only closed bars.

    Evidence weights change with regime and source availability. Forward performance
    can veto an immature/failed symbol-strategy pair, but this layer never submits
    an order and never enables short execution.
    """
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame()
    bar_quality = _bar_quality(df)
    regime = market_regime(df) if not df.empty else "UNKNOWN"
    trend = _trend_evidence(df)
    volume = _volume_evidence(df)
    technical_stability = _technical_stability(df, trend)
    forward_stability = _forward_stability(performance_health)

    external = external_intelligence_assessment(market, strategy)
    sentiment = _clamp(_finite(external.get("external_sentiment_score")), -1.0, 1.0)
    risk = _clamp(_finite(external.get("external_risk_score")), 0.0, 1.0)
    event = _clamp(_finite(external.get("external_event_risk")), 0.0, 1.0)
    external_confidence = _clamp(_finite(external.get("external_confidence")), 0.0, 1.0)
    external_edge = _clamp(0.50 * sentiment - 0.30 * risk - 0.20 * event, -1.0, 1.0)

    weights, evidence_coverage = _adaptive_weights(regime, volume["quality"], external_confidence)
    evidence = {
        "trend": _finite(trend["edge"]),
        "regime": _regime_edge(regime),
        "volume": _finite(volume["edge"]),
        "external": external_edge,
    }
    contributions = {key: weights[key] * evidence[key] for key in weights}
    raw_edge = _clamp(sum(contributions.values()), -1.0, 1.0)
    gross_evidence = sum(abs(value) for value in contributions.values())
    agreement = _clamp(abs(raw_edge) / gross_evidence, 0.0, 1.0) if gross_evidence > 1e-12 else 0.0

    forward_influence = (0.15 + 0.30 * forward_stability["maturity"]) if forward_stability["available"] else 0.0
    stability = _clamp(
        technical_stability["score"] * (1.0 - forward_influence)
        + forward_stability["score"] * forward_influence,
        0.0,
        1.0,
    )
    effective_edge = _clamp(raw_edge * (0.55 + 0.45 * stability) * (0.60 + 0.40 * agreement), -1.0, 1.0)
    structural_edge = _clamp(
        0.55 * evidence["trend"] + 0.25 * evidence["regime"] + 0.20 * evidence["volume"],
        -1.0,
        1.0,
    )

    probability_long = _clamp(_sigmoid(3.0 * effective_edge), 0.08, 0.92)
    probability_short = 1.0 - probability_long
    stop = max(0.005, _finite(stop_distance, 0.03))
    target = max(stop, _finite(target_distance, stop * 1.5))
    reward_risk = _clamp(target / stop, 0.75, 4.0)
    long_ev_r = probability_long * reward_risk - (1.0 - probability_long)
    short_ev_r = probability_short * reward_risk - (1.0 - probability_short)
    ev_gap = abs(long_ev_r - short_ev_r)

    min_ev = 0.08 + 0.12 * (1.0 - stability) + 0.06 * (1.0 - evidence_coverage)
    min_gap = 0.10 + 0.12 * (1.0 - agreement)
    gates: list[str] = []
    if bar_quality["critical"]:
        gates.append("DATA_QUALITY_CRITICAL")
    if trend["quality"] < 0.55:
        gates.append("INSUFFICIENT_PRICE_HISTORY")
    if stability < 0.42:
        gates.append("LOW_STABILITY")
    if agreement < 0.30:
        gates.append("EVIDENCE_CONFLICT")
    if abs(effective_edge) < 0.08:
        gates.append("WEAK_EDGE")
    if effective_edge > 0.0 and structural_edge <= 0.05:
        gates.append("NO_BULLISH_MARKET_CONFIRMATION")
    if effective_edge < 0.0 and structural_edge >= -0.05:
        gates.append("NO_BEARISH_MARKET_CONFIRMATION")
    if forward_stability["maturity"] >= 0.50 and forward_stability["forward_multiplier"] <= 0.25:
        gates.append("FORWARD_HEALTH_PAUSED")

    if not gates and long_ev_r >= min_ev and long_ev_r - short_ev_r >= min_gap:
        direction = "LONG"
    elif not gates and short_ev_r >= min_ev and short_ev_r - long_ev_r >= min_gap:
        direction = "SHORT"
    else:
        direction = "NO_TRADE"
        if not gates:
            gates.append("EV_NOT_DECISIVE")

    confidence = _clamp(
        0.30 * abs(effective_edge)
        + 0.25 * stability
        + 0.20 * agreement
        + 0.15 * evidence_coverage
        + 0.10 * trend["quality"],
        0.0,
        1.0,
    )
    return {
        "direction": direction,
        "direction_confidence": confidence,
        "decision_reasons": gates if direction == "NO_TRADE" else ["ADAPTIVE_EVIDENCE_CONFIRMED"],
        "preferred_playbook": _preferred_playbook(direction, regime, volume, event),
        "long_probability_proxy": probability_long,
        "short_probability_proxy": probability_short,
        "long_ev_proxy_r": float(long_ev_r),
        "short_ev_proxy_r": float(short_ev_r),
        "ev_gap_r": float(ev_gap),
        "min_required_ev_r": float(min_ev),
        "min_required_gap_r": float(min_gap),
        "direction_edge": float(effective_edge),
        "raw_direction_edge": float(raw_edge),
        "structural_direction_edge": float(structural_edge),
        "trend_edge": float(evidence["trend"]),
        "regime_edge": float(evidence["regime"]),
        "volume_edge": float(evidence["volume"]),
        "external_edge": float(evidence["external"]),
        "adaptive_weights": weights,
        "evidence_contributions": contributions,
        "evidence_agreement": float(agreement),
        "evidence_coverage": float(evidence_coverage),
        "stability_score": float(stability),
        "technical_stability": technical_stability,
        "forward_stability": forward_stability,
        "volume_evidence": volume,
        "bar_data_quality": bar_quality,
        "regime": regime,
        "external_risk_regime": external.get("external_risk_regime"),
        "external_sentiment_score": sentiment,
        "external_risk_score": risk,
        "external_event_risk": event,
        "external_confidence": external_confidence,
        "shadow_only": True,
        "broker_order_api_calls": 0,
        "short_execution_enabled": False,
        "ev_type": "ADAPTIVE_DIRECTIONAL_PROXY_UNTIL_FORWARD_CALIBRATION",
        "uses_closed_bars_only": True,
    }
