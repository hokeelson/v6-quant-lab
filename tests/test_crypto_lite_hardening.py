from __future__ import annotations

import sqlite3
from pathlib import Path

from src.realtime_layer import execute_realtime_protective_exit
from src.simulation_db import SimulationDB
from local_backup_worker import run_backup


def _position(symbol="BTCUSDT"):
    return {
        "account_id": "crypto",
        "symbol": symbol,
        "qty": 1.0,
        "avg_entry": 100.0,
        "entry_bar": "2026-09-04T00:00:00+00:00",
        "strategy": "Momentum",
        "horizon": "short",
        "regime_entry": "trend",
        "stop_price": 90.0,
        "target_price": 120.0,
        "max_holding_bars": 10,
        "bars_held": 1,
        "leverage_at_entry": 1.0,
    }


def test_realtime_stop_closes_simulated_position_atomically(tmp_path, monkeypatch):
    db = SimulationDB(str(tmp_path / "simulation_lab.sqlite3"))
    db.ensure_crypto_master_account(100000.0)
    db.set_cash("crypto", 99900.0)
    db.upsert_position(_position())
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")

    trade = execute_realtime_protective_exit(
        db,
        "crypto",
        "BTCUSDT",
        {"price": 89.0, "ts": "2026-09-04T01:00:00+00:00"},
    )
    assert trade is not None
    assert trade["exit_reason"] == "REALTIME_ATR_STOP"
    assert db.position("crypto", "BTCUSDT") is None
    assert len(db.recent_trades(10)) == 1

    # A second tick cannot double-close the same position.
    again = execute_realtime_protective_exit(
        db,
        "crypto",
        "BTCUSDT",
        {"price": 88.0, "ts": "2026-09-04T01:00:01+00:00"},
    )
    assert again is None
    assert len(db.recent_trades(10)) == 1


def test_realtime_target_closes_simulated_position(tmp_path, monkeypatch):
    db = SimulationDB(str(tmp_path / "simulation_lab.sqlite3"))
    db.ensure_crypto_master_account(100000.0)
    db.set_cash("crypto", 99900.0)
    db.upsert_position(_position("ETHUSDT"))
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")

    trade = execute_realtime_protective_exit(
        db,
        "crypto",
        "ETHUSDT",
        {"price": 121.0, "ts": "2026-09-04T01:00:00+00:00"},
    )
    assert trade is not None
    assert trade["exit_reason"] == "REALTIME_ATR_TARGET"


def test_local_backup_creates_valid_sqlite_copy(tmp_path):
    source = tmp_path / "simulation_lab.sqlite3"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE sample(value INTEGER)")
    con.execute("INSERT INTO sample VALUES(7)")
    con.commit()
    con.close()

    result = run_backup(tmp_path)
    backup = Path(result["backup_dir"]) / "simulation_lab.sqlite3"
    assert backup.exists()
    con = sqlite3.connect(backup)
    assert con.execute("SELECT value FROM sample").fetchone()[0] == 7
    con.close()


def test_local_launcher_includes_backup_and_binance_workers():
    text = Path("local_crypto_lite.py").read_text(encoding="utf-8")
    assert "local_backup_worker.py" in text
    assert "binance_market_context_worker.py" in text
