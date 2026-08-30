from src.portfolio_ev import portfolio_ev_summary, rank_portfolio_ev


def _c(symbol, ev_r, ev_pct=0.02, evidence=1.0, confidence=75, corr=0.0, exposure=0.0):
    return {
        "symbol": symbol,
        "market": "crypto",
        "horizon": "short",
        "strategy": "Momentum",
        "expected_value_r": ev_r,
        "expected_value_pct": ev_pct,
        "evidence_weight": evidence,
        "confidence": confidence,
        "correlation_penalty": corr,
        "exposure_penalty": exposure,
    }


def test_higher_ev_r_ranks_first_when_risk_is_equal():
    ranked = rank_portfolio_ev([_c("AAA", 0.25), _c("BBB", 0.60)])
    assert [x["symbol"] for x in ranked] == ["BBB", "AAA"]


def test_correlation_and_exposure_reduce_portfolio_ev_score():
    clean = _c("AAA", 0.50)
    crowded = _c("BBB", 0.50, corr=0.8, exposure=0.8)
    ranked = rank_portfolio_ev([crowded, clean])
    assert ranked[0]["symbol"] == "AAA"
    assert ranked[0]["portfolio_ev_score"] > ranked[1]["portfolio_ev_score"]


def test_immature_evidence_is_discounted_not_deleted():
    mature = _c("AAA", 0.40, evidence=1.0)
    immature = _c("BBB", 0.40, evidence=0.0)
    ranked = rank_portfolio_ev([immature, mature])
    assert ranked[0]["symbol"] == "AAA"
    assert len(ranked) == 2


def test_negative_ev_is_excluded_by_default():
    ranked = rank_portfolio_ev([_c("NEG", -0.10, ev_pct=-0.01), _c("POS", 0.10)])
    assert [x["symbol"] for x in ranked] == ["POS"]


def test_summary_is_observational_and_broker_safe():
    snap = portfolio_ev_summary([_c("AAA", 0.30)])
    assert snap["simulation_only"] is True
    assert snap["broker_order_api_calls"] == 0
    assert snap["top_candidate"]["symbol"] == "AAA"
