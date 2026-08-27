from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from src.crypto_v2.research import (
    ResearchCryptoV2ShadowDB,
    manage_blocked_candidate,
    market_context,
    research_summary,
    session_label,
    update_position_excursion,
)


def bars(prices, start="2026-08-20", freq="1h"):
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    p = np.asarray(prices, dtype=float)
    return pd.DataFrame({
        "open": p,
        "high": p * 1.01,
        "low": p * 0.99,
        "close": p,
        "volume": np.full(len(p), 1000.0),
    }, index=idx)


def test_research_session_and_market_context_are_observation_only():
    assert session_label("2026-08-27T02:00:00+00:00") == "ASIA"
    assert session_label("2026-08-27T10:00:00+00:00") == "EUROPE"
    assert session_label("2026-08-27T14:00:00+00:00") == "EU_US_OVERLAP"
    assert session_label("2026-08-27T20:00:00+00:00") == "US"

    btc = bars(np.linspace(100, 110, 40))
    frames = {
        ("BTCUSDT", "short"): btc,
        ("ETHUSDT", "short"): bars(np.linspace(50, 60, 40)),
        ("SOLUSDT", "short"): bars(np.linspace(20, 25, 40)),
    }
    ctx = market_context(btc.index[-1], "short", frames, btc)
    assert ctx["symbols_observed"] == 3
    assert ctx["breadth_above_ema20"] is not None
    assert ctx["funding_rate"] is None
    assert ctx["external_derivatives_data"] == "NOT_CONNECTED"


def test_research_trade_excursion_round_trip_does_not_change_accounting(tmp_path):
    db = ResearchCryptoV2ShadowDB(str(tmp_path / "v2.sqlite3"), initial_equity=100000.0)
    db.set_research_context({
        "session": "ASIA",
        "breadth_above_ema20": 0.62,
        "avg_pairwise_correlation": 0.71,
        "btc_return_24h": -0.01,
    })
    decision = {
        "action": "ENTER",
        "strategy": "V2_MEAN_REVERSION",
        "confidence": 0.7,
        "stop_distance": 0.03,
        "target_distance": 0.06,
        "max_holding_bars": 8,
        "reason": "test",
    }
    regime = {"state": "SIDEWAYS"}
    did = db.add_decision("BTCUSDT", "short", "2026-08-27T01:00:00+00:00", decision, regime, {"ready": True})
    db.add_buy_order("BTCUSDT", "short", "2026-08-27T01:00:00+00:00", 5000.0, did)
    order = db.pending_order("BTCUSDT", "short")
    assert db.fill_buy(order, "2026-08-27T02:00:00+00:00", 100.0, 0.0019, decision, regime)

    update_position_excursion(db, "BTCUSDT", "short", 108.0, 96.0)
    assert db.close_position("BTCUSDT", "short", "2026-08-27T03:00:00+00:00", 105.0, 0.0019, "TIME")

    summary = research_summary(db)
    exc = summary["trade_excursion_tracking"]
    assert exc["tracked_closed_trades"] == 1
    assert exc["avg_mfe_pct"] >= 0.079
    assert exc["avg_mae_pct"] <= -0.039
    assert exc["by_session"]["ASIA"]["closed_trades"] == 1
    assert len(db.recent_trades()) == 1


def test_risk_block_counterfactual_never_touches_cash_or_positions(tmp_path):
    db = ResearchCryptoV2ShadowDB(str(tmp_path / "v2.sqlite3"), initial_equity=100000.0)
    db.set_research_context({"session": "US"})
    before_cash = db.account("short")["cash"]
    decision = {
        "action": "NO_TRADE",
        "strategy": "V2_MEAN_REVERSION",
        "confidence": 0.7,
        "stop_distance": 0.03,
        "target_distance": 0.06,
        "max_holding_bars": 2,
        "reason": "Portfolio risk governor blocked entry: MAX_STRATEGY_EXPOSURE",
    }
    db.add_decision(
        "ETHUSDT", "short", "2026-08-27T01:00:00+00:00", decision,
        {"state": "SIDEWAYS"}, {"ready": True},
    )

    # Next bar counterfactual enters at 100 and hits its 3% stop. No real V2
    # order, cash movement, or position may be created.
    row = SimpleNamespace(open=100.0, high=101.0, low=96.0, close=97.0)
    manage_blocked_candidate(db, "ETHUSDT", "short", "2026-08-27T02:00:00+00:00", row, 0.0019)

    summary = research_summary(db)
    cf = summary["risk_block_counterfactual"]
    assert cf["closed_candidates"] == 1
    assert cf["avoided_losses"] == 1
    assert cf["avoided_loss_rate"] == 1.0
    assert db.account("short")["cash"] == before_cash
    assert db.position("ETHUSDT", "short") is None
    assert db.pending_order("ETHUSDT", "short") is None
