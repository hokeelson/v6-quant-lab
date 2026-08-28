from __future__ import annotations

from types import SimpleNamespace

from src.crypto_v2.bidirectional_shadow import (
    advance_bidirectional_evaluations,
    bidirectional_summary,
    record_bidirectional_decision,
    score_bidirectional_decision,
)
from src.crypto_v2.research import ResearchCryptoV2ShadowDB


FEE = 0.0019


def features(**overrides):
    base = {
        "ready": True,
        "atr_pct": 0.02,
        "ret_fast": 0.0,
        "ret_slow": 0.0,
        "relative_strength": 0.0,
        "zscore20": 0.0,
        "volume_z": 0.0,
    }
    base.update(overrides)
    return base


def test_bidirectional_shadow_can_choose_long_short_or_no_trade():
    long_case = score_bidirectional_decision(
        {"state": "TREND_UP"},
        features(ret_fast=0.03, ret_slow=0.06, relative_strength=0.02),
        "short",
        {"breadth_above_ema20": 0.70, "btc_return_24h": 0.04, "avg_pairwise_correlation": 0.40},
        FEE,
    )
    assert long_case["selected_action"] == "LONG"
    assert long_case["long_ev_proxy"] > long_case["short_ev_proxy"]
    assert long_case["ev_is_calibrated"] is False

    short_case = score_bidirectional_decision(
        {"state": "TREND_DOWN"},
        features(ret_fast=-0.03, ret_slow=-0.06, relative_strength=-0.02),
        "short",
        {"breadth_above_ema20": 0.25, "btc_return_24h": -0.04, "avg_pairwise_correlation": 0.40},
        FEE,
    )
    assert short_case["selected_action"] == "SHORT"
    assert short_case["short_ev_proxy"] > short_case["long_ev_proxy"]

    no_trade = score_bidirectional_decision(
        {"state": "HIGH_VOL_SIDEWAYS"},
        features(ret_fast=0.02, ret_slow=-0.01, zscore20=2.2),
        "short",
        {"breadth_above_ema20": 0.50, "btc_return_24h": 0.0, "avg_pairwise_correlation": 0.90},
        FEE,
    )
    assert no_trade["selected_action"] == "NO_TRADE"


def test_forward_evaluation_enters_next_bar_and_never_touches_v2_accounting(tmp_path):
    db = ResearchCryptoV2ShadowDB(str(tmp_path / "v2.sqlite3"), initial_equity=100000.0)
    before_cash = float(db.account("short")["cash"])
    decision_bar = "2026-08-29T00:00:00+00:00"
    scored = record_bidirectional_decision(
        db,
        "BTCUSDT",
        "short",
        decision_bar,
        {"state": "TREND_UP"},
        features(ret_fast=0.03, ret_slow=0.05, relative_strength=0.02),
        {"session": "ASIA", "breadth_above_ema20": 0.70, "btc_return_24h": 0.03},
        FEE,
    )
    assert scored["selected_action"] == "LONG"

    # Same bar must not become the entry bar: the decision did not exist at its open.
    same_bar = SimpleNamespace(open=100.0, high=101.0, low=99.0, close=100.5)
    advance_bidirectional_evaluations(db, "BTCUSDT", "short", decision_bar, same_bar, FEE)
    with db._c() as c:
        row = dict(c.execute("SELECT * FROM bidirectional_decisions").fetchone())
    assert row["status"] == "PENDING_ENTRY"
    assert row["entry_bar"] is None

    # Next bar opens the purely counterfactual evaluation. Six bars close it.
    for i in range(1, 7):
        stamp = f"2026-08-29T0{i}:00:00+00:00"
        px = 100.0 + i
        bar = SimpleNamespace(open=100.0 if i == 1 else px - 0.5, high=px + 1.0, low=px - 1.0, close=px)
        advance_bidirectional_evaluations(db, "BTCUSDT", "short", stamp, bar, FEE)

    with db._c() as c:
        row = dict(c.execute("SELECT * FROM bidirectional_decisions").fetchone())
    assert row["status"] == "CLOSED"
    assert row["entry_bar"] == "2026-08-29T01:00:00+00:00"
    assert float(row["entry_price"]) == 100.0
    assert row["best_realized_action"] == "LONG"
    assert int(row["decision_correct"]) == 1

    # Research shadow can never create V2 orders/positions or move account cash.
    assert float(db.account("short")["cash"]) == before_cash
    assert db.position("BTCUSDT", "short") is None
    assert db.pending_order("BTCUSDT", "short") is None


def test_no_trade_is_a_real_forward_outcome_not_missing_signal(tmp_path):
    db = ResearchCryptoV2ShadowDB(str(tmp_path / "v2.sqlite3"), initial_equity=100000.0)
    record_bidirectional_decision(
        db,
        "ETHUSDT",
        "short",
        "2026-08-29T00:00:00+00:00",
        {"state": "PANIC"},
        features(ret_fast=-0.05, ret_slow=-0.08),
        {"session": "ASIA", "breadth_above_ema20": 0.20, "btc_return_24h": -0.08},
        FEE,
    )

    # Flat market makes both traded alternatives negative after round-trip costs,
    # therefore NO_TRADE is correctly scored as the best realized action.
    for i in range(1, 7):
        stamp = f"2026-08-29T0{i}:00:00+00:00"
        bar = SimpleNamespace(open=100.0, high=100.2, low=99.8, close=100.0)
        advance_bidirectional_evaluations(db, "ETHUSDT", "short", stamp, bar, FEE)

    summary = bidirectional_summary(db)
    assert summary["closed_evaluations"] == 1
    assert summary["decision_accuracy"] == 1.0
    assert summary["no_trade_avoided_losses"] == 1
    assert summary["changes_v2_execution"] is False
