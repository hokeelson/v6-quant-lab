from src.risk_sizing import _portfolio_ev_multiplier, _trade_ev_multiplier


def test_negative_ev_is_reduced_but_kept_for_shadow_learning():
    mult, state = _trade_ev_multiplier(-0.01, -0.20, 1.0)
    assert state == "NEGATIVE_EV"
    assert mult == 0.25


def test_immature_negative_ev_is_not_over_penalized():
    mult, state = _trade_ev_multiplier(-0.001, -0.02, 0.10)
    assert state == "NEGATIVE_EV"
    assert mult == 0.50


def test_strong_positive_ev_keeps_full_size():
    mult, state = _trade_ev_multiplier(0.02, 0.60, 0.80)
    assert state == "STRONG_POSITIVE_EV"
    assert mult == 1.0


def test_portfolio_score_reduces_low_quality_candidate():
    mult, state = _portfolio_ev_multiplier(0.05, True)
    assert state == "LOW"
    assert mult == 0.70


def test_negative_portfolio_ev_never_gets_full_size():
    mult, state = _portfolio_ev_multiplier(-0.10, False)
    assert state == "NEGATIVE_EV_PORTFOLIO"
    assert mult == 0.50
