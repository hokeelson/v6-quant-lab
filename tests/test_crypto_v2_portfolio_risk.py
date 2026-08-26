from __future__ import annotations

from src.crypto_v2.risk import govern_entry, portfolio_status
from src.crypto_v2.shadow_db import CryptoV2ShadowDB


def test_portfolio_governor_counts_pending_and_strategy_exposure(tmp_path):
    db = CryptoV2ShadowDB(str(tmp_path / "crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
    decision = {
        "action": "ENTER",
        "strategy": "V2_MEAN_REVERSION",
        "confidence": 0.7,
        "reason": "test",
    }
    regime = {"state": "SIDEWAYS"}

    # Reserve four 6k orders. They must count before fill so a burst of signals
    # cannot create more same-strategy exposure than the portfolio cap.
    for i, symbol in enumerate(("AUSDT", "BUSDT", "CUSDT", "DUSDT")):
        bar = f"2026-08-26T0{i}:00:00+00:00"
        did = db.add_decision(symbol, "short", bar, decision, regime, {"ready": True})
        db.add_buy_order(symbol, "short", bar, 6000.0, did)

    state = db.portfolio_state("short")
    status = portfolio_status(100000.0, state, "short")
    assert status["pending_orders"] == 4
    assert status["gross_notional"] == 24000.0
    assert status["strategy_notional"]["V2_MEAN_REVERSION"] == 24000.0

    result = govern_entry(
        100000.0,
        6000.0,
        state,
        "short",
        "V2_MEAN_REVERSION",
        "SIDEWAYS",
    )
    # Same-strategy cap is 25k, leaving only 1k room: the 0.5% minimum-entry
    # check keeps the system from opening a meaningless dust position.
    assert result["approved_notional"] == 1000.0
    assert result["reason"] == "DOWNSIZED_BY_PORTFOLIO_RISK"


def test_portfolio_governor_blocks_when_total_cap_is_full():
    state = {
        "positions": [
            {"notional": 12000.0, "strategy": "V2_MOMENTUM", "regime": "TREND_UP"},
            {"notional": 12000.0, "strategy": "V2_BREAKOUT", "regime": "TREND_UP"},
            {"notional": 11000.0, "strategy": "V2_MEAN_REVERSION", "regime": "SIDEWAYS"},
        ],
        "pending_orders": [],
    }
    result = govern_entry(
        100000.0,
        5000.0,
        state,
        "short",
        "V2_MEAN_REVERSION",
        "SIDEWAYS",
    )
    assert result["approved_notional"] == 0.0
    assert result["reason"] == "MAX_GROSS_EXPOSURE"


def test_portfolio_governor_blocks_when_position_slots_are_full():
    state = {
        "positions": [
            {"notional": 4000.0, "strategy": f"S{i}", "regime": f"R{i}"}
            for i in range(6)
        ],
        "pending_orders": [],
    }
    result = govern_entry(100000.0, 5000.0, state, "short", "NEW", "NEW")
    assert result["approved_notional"] == 0.0
    assert result["reason"] == "MAX_POSITIONS"
