from src.trade_ev import estimate_trade_ev


def test_ev_positive_when_reward_and_evidence_support_entry():
    ev = estimate_trade_ev(
        oos_metrics={"closed_trades": 40, "win_rate": 0.60},
        trade_confidence=75,
        regime_fit=0.8,
        stop_distance=0.03,
        target_distance=0.07,
        buy_cost_rate=0.001,
        sell_cost_rate=0.001,
    )
    assert 0.5 < ev.probability_win < 0.9
    assert ev.expected_value_pct > 0
    assert ev.expected_value_r > 0
    assert ev.evidence_weight == 1.0


def test_ev_shrinks_small_sample_toward_neutral():
    ev = estimate_trade_ev(
        oos_metrics={"closed_trades": 1, "win_rate": 1.0},
        trade_confidence=50,
        regime_fit=0.5,
        stop_distance=0.03,
        target_distance=0.03,
        buy_cost_rate=0,
        sell_cost_rate=0,
    )
    assert 0.50 < ev.probability_win < 0.55
    assert ev.evidence_weight < 0.1


def test_costs_reduce_ev():
    base = dict(
        oos_metrics={"closed_trades": 30, "win_rate": 0.55},
        trade_confidence=70,
        regime_fit=0.7,
        stop_distance=0.03,
        target_distance=0.06,
    )
    low = estimate_trade_ev(**base, buy_cost_rate=0, sell_cost_rate=0)
    high = estimate_trade_ev(**base, buy_cost_rate=0.005, sell_cost_rate=0.005)
    assert high.expected_value_pct < low.expected_value_pct
