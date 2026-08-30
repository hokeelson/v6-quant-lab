from src.ev_threshold_experiment import threshold_bucket
from src.expected_live_sizing import _should_quarantine
from src.policy_epoch import is_post_epoch
from src.research import robustness_score


def test_policy_epoch_boundary():
    assert not is_post_epoch("2026-08-30T11:45:59+00:00")
    assert is_post_epoch("2026-08-30T11:46:00+00:00")


def test_thin_oos_sample_is_capped():
    metrics = {
        "sharpe": 12.0,
        "sortino": 20.0,
        "calmar": 15.0,
        "profit_factor": 16.0,
        "max_drawdown": -0.01,
        "closed_trades": 4,
    }
    assert robustness_score(metrics, min_trades=20) <= 52.0


def test_quarantine_requires_mature_severe_sign_reversal():
    row = {
        "live_closed_trades": 15,
        "state": "SEVERE_DIVERGENCE",
        "reasons": [
            "OOS_POSITIVE_LIVE_NEGATIVE",
            "EXPECTANCY_SIGN_REVERSAL",
            "PROFIT_FACTOR_DETERIORATION",
        ],
    }
    assert _should_quarantine(row)
    row["live_closed_trades"] = 5
    assert not _should_quarantine(row)


def test_ev_threshold_buckets():
    row = threshold_bucket(0.22)
    assert row["passed"]["ev_r_ge_0.20"] is True
    assert row["passed"]["ev_r_ge_0.30"] is False
    assert row["broker_order_api_calls"] == 0
