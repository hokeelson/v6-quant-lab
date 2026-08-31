import numpy as np
import pandas as pd

import src.direction_engine as de


def _df(up=True, n=120):
    base = np.linspace(100, 140 if up else 70, n)
    close = pd.Series(base)
    return pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
    })


def _neutral_external(*args, **kwargs):
    return {
        "external_sentiment_score": 0.0,
        "external_risk_score": 0.0,
        "external_event_risk": 0.0,
        "external_confidence": 1.0,
        "external_risk_regime": "NORMAL",
    }


def test_uptrend_prefers_long(monkeypatch):
    monkeypatch.setattr(de, "external_intelligence_assessment", _neutral_external)
    out = de.assess_direction(_df(True), "stock", "Trend MA", 0.04, 0.08)
    assert out["long_ev_proxy_r"] > out["short_ev_proxy_r"]
    assert out["direction"] == "LONG"


def test_downtrend_prefers_short(monkeypatch):
    monkeypatch.setattr(de, "external_intelligence_assessment", _neutral_external)
    out = de.assess_direction(_df(False), "crypto", "Momentum", 0.04, 0.08)
    assert out["short_ev_proxy_r"] > out["long_ev_proxy_r"]
    assert out["direction"] == "SHORT"


def test_short_layer_never_enables_execution(monkeypatch):
    monkeypatch.setattr(de, "external_intelligence_assessment", _neutral_external)
    out = de.assess_direction(_df(False), "stock", "Momentum", 0.04, 0.08)
    assert out["shadow_only"] is True
    assert out["short_execution_enabled"] is False
    assert out["broker_order_api_calls"] == 0
