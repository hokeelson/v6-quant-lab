import pandas as pd

from src.simulation_db import SimulationDB
from src.simulation_engine import SimulationLab


def _position(aid="stock_short", symbol="TEST"):
    return {
        "account_id": aid, "symbol": symbol, "qty": 10.0, "avg_entry": 100.0,
        "entry_bar": "2026-01-01T00:00:00+00:00", "strategy": "trend_ma", "horizon": "short",
        "regime_entry": "bull", "stop_price": 90.0, "target_price": 130.0,
        "max_holding_bars": 100, "bars_held": 0, "leverage_at_entry": 1.0,
    }


def test_stale_buy_is_cancelled(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3"))
    db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "BUY", "created_bar": "2026-01-01T00:00:00+00:00", "requested_notional": 1000, "qty": None, "reason": "TEST", "decision_id": None})
    lab = SimulationLab(db=db)
    result = lab._execute_pending(aid, "stock", symbol, pd.Timestamp("2026-01-02", tz="UTC"), pd.Series({"open": 100.0}))
    assert result == "CANCELLED"
    with db._c() as c:
        row = c.execute("SELECT status,reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert row["status"] == "CANCELLED"
    assert row["reason"] == "STALE_BUY_POSITION_EXISTS"


def test_gap_stop_uses_open_and_closes_atomically(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3"))
    db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    lab = SimulationLab(db=db)
    result = lab._protect_position(aid, "stock", symbol, pd.Timestamp("2026-01-02", tz="UTC"), pd.Series({"open": 80.0, "high": 85.0, "low": 75.0, "close": 82.0}))
    assert result == "ATR_STOP_GAP"
    assert db.position(aid, symbol) is None
    trades = db.recent_trades(10)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "ATR_STOP_GAP"
    assert trades[0]["exit_price"] < 81.0


def test_atomic_sell_is_idempotent(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3"))
    db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00", "requested_notional": 0, "qty": 10, "reason": "TEST", "decision_id": None})
    trade = {"account_id": aid, "symbol": symbol, "entry_bar": "2026-01-01T00:00:00+00:00", "exit_bar": "2026-01-02T00:00:00+00:00", "qty": 10, "entry_price": 100, "exit_price": 110, "realized_pnl": 100, "return_pct": 0.1, "strategy": "trend_ma", "horizon": "short", "regime_entry": "bull", "exit_reason": "TEST", "leverage": 1.0}
    assert db.fill_sell_atomic(aid, oid, trade["exit_bar"], 110, 0, 0, 101100, trade, symbol) is True
    assert db.fill_sell_atomic(aid, oid, trade["exit_bar"], 110, 0, 0, 102200, trade, symbol) is False
    assert len(db.recent_trades(10)) == 1
    assert db.position(aid, symbol) is None
