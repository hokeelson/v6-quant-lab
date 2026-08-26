import os
import sqlite3

from src.backtest import ExecutionCosts
from src.simulation_db import SCHEMA_VERSION, SimulationDB
from src.twstock_support import _tw_costs


def _trade(aid="stock_short", symbol="TEST"):
    return {
        "account_id": aid, "symbol": symbol,
        "entry_bar": "2026-01-01T00:00:00+00:00", "exit_bar": "2026-01-02T00:00:00+00:00",
        "qty": 10.0, "entry_price": 100.0, "exit_price": 110.0,
        "realized_pnl": 100.0, "return_pct": 0.1, "strategy": "trend_ma",
        "horizon": "short", "regime_entry": "bull", "exit_reason": "TEST", "leverage": 1.0,
    }


def _position(aid="stock_short", symbol="TEST"):
    return {
        "account_id": aid, "symbol": symbol, "qty": 10.0, "avg_entry": 100.0,
        "entry_bar": "2026-01-01T00:00:00+00:00", "strategy": "trend_ma", "horizon": "short",
        "regime_entry": "bull", "stop_price": 90.0, "target_price": 130.0,
        "max_holding_bars": 100, "bars_held": 0, "leverage_at_entry": 1.0,
    }


def test_schema_migrates_legacy_trades_table(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades(trade_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL, entry_bar TEXT NOT NULL, exit_bar TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL, realized_pnl REAL NOT NULL, return_pct REAL NOT NULL, strategy TEXT, horizon TEXT, regime_entry TEXT, exit_reason TEXT, leverage REAL, created_at TEXT NOT NULL)")
    con.commit(); con.close()
    db = SimulationDB(str(path))
    with db._c() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        version = c.execute("PRAGMA user_version").fetchone()[0]
    assert "exit_order_id" in cols
    assert version == SCHEMA_VERSION


def test_sell_trade_records_unique_exit_order_id(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3")); db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00", "requested_notional": 0, "qty": 10, "reason": "TEST", "decision_id": None})
    assert db.fill_sell_atomic(aid, oid, "2026-01-02T00:00:00+00:00", 110, 0, 0, 101100, _trade(aid, symbol), symbol)
    row = db.recent_trades(1)[0]
    assert row["exit_order_id"] == oid


def test_taiwan_cost_is_asymmetric():
    c = _tw_costs()
    assert isinstance(c, ExecutionCosts)
    assert round(c.sell_rate - c.buy_rate, 8) == 0.003
    assert c.buy_rate < c.one_way_rate < c.sell_rate


def test_trade_checkpoint_writes_persistent_snapshot(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    persistent = tmp_path / "persistent"; persistent.mkdir()
    monkeypatch.setenv("V6_PERSISTENT_DATA_DIR", str(persistent))
    path = runtime / "simulation_lab.sqlite3"
    db = SimulationDB(str(path)); db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00", "requested_notional": 0, "qty": 10, "reason": "TEST", "decision_id": None})
    assert db.fill_sell_atomic(aid, oid, "2026-01-02T00:00:00+00:00", 110, 0, 0, 101100, _trade(aid, symbol), symbol)
    snap = persistent / "v6-snapshots" / "current" / "simulation_lab.sqlite3"
    assert snap.exists() and snap.stat().st_size > 0
    con = sqlite3.connect(snap)
    assert con.execute("PRAGMA quick_check").fetchone()[0].lower() == "ok"
    assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    con.close()
