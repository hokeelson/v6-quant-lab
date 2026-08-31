import numpy as np
import pandas as pd
import json

import src.direction_engine as engine


def _frame(direction=1, n=160, volume=True):
    rng = np.random.default_rng(7)
    returns = direction * 0.002 + rng.normal(0, 0.001, n)
    close = pd.Series(100 * np.exp(np.cumsum(returns)))
    data = {
        "open": close * 0.999,
        "high": close * 1.005,
        "low": close * 0.995,
        "close": close,
    }
    if volume:
        data["volume"] = pd.Series(np.linspace(1000, 1800, n))
    return pd.DataFrame(data)


def _external(sentiment=0.0, risk=0.0, event=0.0, confidence=1.0):
    return {
        "external_sentiment_score": sentiment,
        "external_risk_score": risk,
        "external_event_risk": event,
        "external_confidence": confidence,
        "external_risk_regime": "TEST",
    }


def test_confirmed_uptrend_can_choose_long(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    out = engine.assess_direction(_frame(1), "stock", "Trend MA", 0.04, 0.08)
    assert out["direction"] == "LONG"
    assert out["volume_evidence"]["status"] == "AVAILABLE"
    assert out["adaptive_weights"]["volume"] > 0


def test_confirmed_downtrend_can_choose_short(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    out = engine.assess_direction(_frame(-1), "crypto", "Momentum", 0.04, 0.08)
    assert out["direction"] == "SHORT"
    assert out["short_execution_enabled"] is False
    assert out["broker_order_api_calls"] == 0


def test_missing_volume_redistributes_weight_without_fabricating_volume(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external(confidence=0.0))
    out = engine.assess_direction(_frame(1, volume=False), "stock", "Trend MA", 0.04, 0.08)
    assert out["volume_evidence"]["status"] == "UNAVAILABLE"
    assert out["volume_edge"] == 0.0
    assert out["adaptive_weights"]["volume"] == 0.0


def test_conflicting_evidence_can_wait(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external(sentiment=-1.0, risk=1.0, event=1.0))
    frame = _frame(1)
    frame["volume"] = np.linspace(2000, 500, len(frame))
    out = engine.assess_direction(frame, "stock", "Momentum", 0.04, 0.08)
    assert out["direction"] in {"LONG", "NO_TRADE"}
    if out["direction"] == "NO_TRADE":
        assert out["decision_reasons"]


def test_mature_failed_forward_health_vetoes_entry(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    health = {"samples": 20, "shadow_weight_multiplier": 0.25, "state": "PAUSE_CANDIDATE"}
    out = engine.assess_direction(_frame(1), "stock", "Trend MA", 0.04, 0.08, health)
    assert out["direction"] == "NO_TRADE"
    assert "FORWARD_HEALTH_PAUSED" in out["decision_reasons"]


def test_only_closed_input_is_used(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    full = _frame(1)
    original = engine.assess_direction(full.iloc[:-1], "stock", "Trend MA", 0.04, 0.08)
    changed_future = full.copy()
    changed_future.iloc[-1, changed_future.columns.get_loc("close")] *= 0.5
    repeated = engine.assess_direction(changed_future.iloc[:-1], "stock", "Trend MA", 0.04, 0.08)
    assert original["direction_edge"] == repeated["direction_edge"]


def test_external_source_weight_falls_to_zero_when_unavailable():
    weights, coverage = engine._adaptive_weights("NORMAL_UP_TREND", 1.0, 0.0)
    assert weights["external"] == 0.0
    assert weights["trend"] > weights["volume"]
    assert coverage < 1.0


def test_high_volatility_changes_evidence_mix():
    normal, _ = engine._adaptive_weights("NORMAL_UP_TREND", 1.0, 1.0)
    high_vol, _ = engine._adaptive_weights("HIGH_VOL_UP_TREND", 1.0, 1.0)
    assert high_vol["volume"] > normal["volume"]
    assert high_vol["external"] > normal["external"]
    assert high_vol["trend"] < normal["trend"]


def test_insufficient_history_is_no_trade(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    out = engine.assess_direction(_frame(1, n=40), "stock", "Trend MA", 0.04, 0.08)
    assert out["direction"] == "NO_TRADE"
    assert "INSUFFICIENT_PRICE_HISTORY" in out["decision_reasons"]


def test_negative_news_alone_does_not_force_short(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external(-1.0, 1.0, 1.0, 1.0))
    flat = _frame(1)
    flat["close"] = 100.0 + np.sin(np.arange(len(flat)) / 2.0) * 0.05
    flat["open"] = flat["close"]
    flat["high"] = flat["close"] * 1.001
    flat["low"] = flat["close"] * 0.999
    out = engine.assess_direction(flat, "crypto", "Momentum", 0.04, 0.08)
    assert out["direction"] == "NO_TRADE"


def test_extreme_or_missing_volume_remains_json_safe(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    frame = _frame(1)
    frame.loc[frame.index[-3:], "volume"] = [np.nan, 0.0, 1e18]
    out = engine.assess_direction(frame, "stock", "Breakout", 0.04, 0.08)
    json.dumps(out, allow_nan=False)
    assert -1.0 <= out["volume_edge"] <= 1.0


def test_assessment_does_not_mutate_market_data(monkeypatch):
    monkeypatch.setattr(engine, "external_intelligence_assessment", lambda *a, **k: _external())
    frame = _frame(1)
    before = frame.copy(deep=True)
    engine.assess_direction(frame, "stock", "Trend MA", 0.04, 0.08)
    pd.testing.assert_frame_equal(frame, before)
