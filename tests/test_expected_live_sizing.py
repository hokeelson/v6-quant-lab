from src.expected_live_sizing import (
    blend_expected_live_multiplier,
    forward_shadow_weight,
)


def test_forward_weight_is_zero_before_minimum_samples():
    assert forward_shadow_weight(0) == 0.0
    assert forward_shadow_weight(4) == 0.0


def test_forward_weight_ramps_with_samples():
    w5 = forward_shadow_weight(5)
    w15 = forward_shadow_weight(15)
    w30 = forward_shadow_weight(30)
    assert 0.0 < w5 < w15 < w30
    assert abs(w30 - 0.90) < 1e-12
    assert forward_shadow_weight(100) == 0.90


def test_mature_shadow_evidence_dominates_oos_anchor():
    effective, forward_weight, backtest_weight = blend_expected_live_multiplier(0.40, 30)
    assert abs(forward_weight - 0.90) < 1e-12
    assert abs(backtest_weight - 0.10) < 1e-12
    assert abs(effective - 0.46) < 1e-12


def test_early_shadow_penalty_is_tempered():
    effective, forward_weight, backtest_weight = blend_expected_live_multiplier(0.40, 5)
    assert abs(forward_weight - 0.25) < 1e-12
    assert abs(backtest_weight - 0.75) < 1e-12
    assert abs(effective - 0.85) < 1e-12
