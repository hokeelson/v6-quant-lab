from __future__ import annotations

import json
from datetime import datetime, timezone

from src.external_intelligence import _context, _headline_metrics, external_intelligence_assessment


def test_external_context_never_increases_exposure():
    ctx = _context(sentiment=1.0, event_risk=0.0, stress=0.0, confidence=1.0)
    assert ctx["market_multiplier"] <= 1.0
    assert all(v <= 1.0 for v in ctx["strategy_multipliers"].values())


def test_negative_event_headlines_raise_risk_inputs():
    m = _headline_metrics([
        "Fed hawkish as inflation fears rise",
        "Crypto hack sparks liquidation fear",
        "War and sanctions hit risk assets",
    ])
    assert m["sentiment"] < 0
    assert m["event_risk"] > 0


def test_assessment_uses_strictest_market_or_strategy_multiplier(tmp_path):
    path = tmp_path / "intel.json"
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "AVAILABLE",
        "markets": {
            "crypto": {
                "market_multiplier": 0.70,
                "strategy_multipliers": {"Momentum": 0.60},
                "risk_regime": "RISK_OFF",
                "risk_score": 0.7,
                "sentiment_score": -0.4,
                "event_risk": 0.8,
                "confidence": 0.9,
            }
        },
    }), encoding="utf-8")
    out = external_intelligence_assessment("crypto", "Momentum", path)
    assert out["external_intelligence_multiplier"] == 0.60


def test_unavailable_snapshot_fails_open(tmp_path):
    path = tmp_path / "intel.json"
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "UNAVAILABLE",
        "markets": {},
    }), encoding="utf-8")
    out = external_intelligence_assessment("stock", "Trend MA", path)
    assert out["external_intelligence_multiplier"] == 1.0
