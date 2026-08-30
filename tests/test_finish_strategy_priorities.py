from src.batch_portfolio_optimizer import optimize_batch
from src.regime_allocation import regime_strategy_multiplier


def test_dynamic_regime_prior_prefers_matching_strategy():
    assert regime_strategy_multiplier("Trend MA", "NORMAL_UP_TREND") > regime_strategy_multiplier("Trend MA", "NORMAL_DOWN_TREND")
    assert regime_strategy_multiplier("Mean Reversion RSI", "NORMAL_SIDEWAYS") > regime_strategy_multiplier("Mean Reversion RSI", "HIGH_VOL_SIDEWAYS")
    assert regime_strategy_multiplier("Breakout", "HIGH_VOL_SIDEWAYS") >= 0.95


def test_batch_optimizer_prefers_positive_ev_and_penalizes_correlation():
    candidates = [
        {"candidate_key": "crypto:BTCUSDT:short", "symbol": "BTCUSDT", "market": "crypto", "horizon": "short", "strategy": "Breakout", "expected_value_pct": 0.03, "expected_value_r": 0.60, "evidence_weight": 1.0, "confidence": 80, "correlation_penalty": 0, "exposure_penalty": 0},
        {"candidate_key": "crypto:ETHUSDT:short", "symbol": "ETHUSDT", "market": "crypto", "horizon": "short", "strategy": "Momentum", "expected_value_pct": 0.02, "expected_value_r": 0.45, "evidence_weight": 1.0, "confidence": 78, "correlation_penalty": 0, "exposure_penalty": 0},
        {"candidate_key": "crypto:XRPUSDT:short", "symbol": "XRPUSDT", "market": "crypto", "horizon": "short", "strategy": "Trend MA", "expected_value_pct": -0.01, "expected_value_r": -0.20, "evidence_weight": 1.0, "confidence": 70, "correlation_penalty": 0, "exposure_penalty": 0},
    ]
    pairwise = {tuple(sorted(("BTCUSDT", "ETHUSDT"))): 0.93}
    out = optimize_batch(candidates, pairwise)
    assert out["crypto:BTCUSDT:short"]["batch_ev_multiplier"] >= out["crypto:ETHUSDT:short"]["batch_ev_multiplier"]
    assert out["crypto:ETHUSDT:short"]["batch_ev_cohort_max_correlation"] >= 0.90
    assert out["crypto:XRPUSDT:short"]["batch_ev_multiplier"] <= 0.25
