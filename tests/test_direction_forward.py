from __future__ import annotations

import pandas as pd

from src.direction_forward import DirectionForwardLedger


def _bars(n=20, start=100.0, step=1.0):
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    close = pd.Series([start + step * i for i in range(n)], index=index)
    return pd.DataFrame({
        "open": close,
        "high": close * 1.002,
        "low": close * 0.998,
        "close": close,
        "volume": 1000.0,
    }, index=index)


def _prediction(df, decision="LONG", as_of_index=5, stop=0.04, target=0.08):
    return {
        "market": "stock",
        "symbol": "TEST",
        "horizon": "short",
        "strategy": "adaptive",
        "as_of": df.index[as_of_index].isoformat(),
        "direction": decision,
        "direction_confidence": 0.8,
        "stop_distance": stop,
        "target_distance": target,
        "adaptive_weights": {"trend": 0.5, "volume": 0.5},
    }


def test_prediction_uses_next_bar_open_and_evaluates(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars()
    assert ledger.register(_prediction(df))["registered"] is True
    result = ledger.evaluate_pair(df, "stock", "TEST", "short")
    assert result["evaluated"] == 1
    row = ledger.recent(1)[0]
    assert row["entry_bar"] == df.index[6].isoformat()
    assert row["entry_price"] == df.open.iloc[6]
    assert row["directional_return_pct"] > 0


def test_pending_prediction_blocks_overlapping_samples(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars()
    assert ledger.register(_prediction(df))["registered"] is True
    later = _prediction(df, as_of_index=6)
    assert ledger.register(later)["reason"] == "PENDING_EXISTS"
    assert len(ledger.recent()) == 1


def test_not_enough_future_bars_stays_pending(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars(n=10)
    ledger.register(_prediction(df, as_of_index=7))
    out = ledger.evaluate_pair(df, "stock", "TEST", "short")
    assert out == {"evaluated": 0, "waiting": 1}
    assert ledger.summary()["pending"] == 1


def test_short_direction_profits_from_decline_after_cost(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars(step=-1.0)
    ledger.register(_prediction(df, decision="SHORT"))
    ledger.evaluate_pair(df, "stock", "TEST", "short")
    row = ledger.recent(1)[0]
    assert row["directional_return_pct"] > 0
    assert row["hit"] == 1


def test_no_trade_records_missed_opportunity_without_fake_profit(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars(step=0.01)
    ledger.register(_prediction(df, decision="NO_TRADE"))
    ledger.evaluate_pair(df, "stock", "TEST", "short")
    row = ledger.recent(1)[0]
    assert row["directional_return_pct"] == 0.0
    assert row["opportunity_return_pct"] >= 0.0
    assert row["hit"] == 1


def test_same_bar_stop_and_target_resolves_to_stop(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars(step=0.0)
    prediction = _prediction(df, decision="LONG", stop=0.02, target=0.02)
    ledger.register(prediction)
    entry_index = 6
    df.loc[df.index[entry_index], "low"] = df.open.iloc[entry_index] * 0.97
    df.loc[df.index[entry_index], "high"] = df.open.iloc[entry_index] * 1.03
    ledger.evaluate_pair(df, "stock", "TEST", "short")
    row = ledger.recent(1)[0]
    assert row["exit_reason"] == "STOP"
    assert row["directional_return_pct"] < 0


def test_summary_separates_decisions_and_preserves_safety(tmp_path):
    ledger = DirectionForwardLedger(tmp_path / "direction.sqlite3")
    df = _bars()
    ledger.register(_prediction(df, decision="LONG"))
    ledger.evaluate_pair(df, "stock", "TEST", "short")
    summary = ledger.summary()
    assert summary["evaluated"] == 1
    assert summary["decision_stats"]["LONG"]["completed"] == 1
    assert summary["shadow_only"] is True
    assert summary["broker_order_api_calls"] == 0
