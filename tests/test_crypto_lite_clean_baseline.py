from __future__ import annotations

from datetime import datetime, timezone

from crypto_lite_cleanup import cleanup_simulation
from src.realtime_layer import RealtimeDB, build_realtime_watchlist, _market_from_account_id
from src.simulation_db import SimulationDB
from src.simulation_engine import SimulationLab


class DummyCache:
    pass


def test_single_crypto_mode_does_not_create_legacy_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")
    db = SimulationDB(str(tmp_path / "simulation.sqlite3"))

    SimulationLab(db=db, cache=DummyCache(), initial_equity=100000.0)

    accounts = db.accounts()
    assert [row["account_id"] for row in accounts] == ["crypto"]
    assert accounts[0]["market"] == "crypto"
    assert accounts[0]["horizon"] == "all"
    assert accounts[0]["status"] == "ACTIVE"


def test_cleanup_removes_legacy_financial_rows_but_keeps_master_position(tmp_path):
    path = tmp_path / "simulation.sqlite3"
    db = SimulationDB(str(path))
    db.ensure_accounts(100000.0)
    db.ensure_crypto_master_account(100000.0)
    db.add_asset("stock", "AAPL")
    db.add_asset("crypto", "BTCUSDT")

    with db._c() as con:
        con.execute(
            """INSERT INTO positions(
                account_id,symbol,qty,avg_entry,entry_bar,strategy,horizon,regime_entry,
                stop_price,target_price,max_holding_bars,bars_held,leverage_at_entry
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("crypto", "BTCUSDT", 1.0, 100.0, "2026-09-04T00:00:00+00:00",
             "Trend MA", "medium", "UP", 90.0, 120.0, 20, 0, 1.0),
        )
        con.execute(
            """INSERT INTO positions(
                account_id,symbol,qty,avg_entry,entry_bar,strategy,horizon,regime_entry,
                stop_price,target_price,max_holding_bars,bars_held,leverage_at_entry
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("stock_short", "AAPL", 2.0, 200.0, "2026-09-04T00:00:00+00:00",
             "Momentum", "short", "UP", 190.0, 220.0, 10, 0, 1.0),
        )

    cleanup_simulation(path, datetime.now(timezone.utc).isoformat())

    assert [row["account_id"] for row in db.accounts()] == ["crypto"]
    assert db.position("crypto", "BTCUSDT") is not None
    assert db.position("stock_short", "AAPL") is None
    assert {row["market"] for row in db.assets()} == {"crypto"}


class FakeRealtimeSource:
    def positions(self):
        return [
            {"account_id": "crypto", "symbol": "BTCUSDT"},
            {"account_id": "stock_short", "symbol": "AAPL"},
        ]

    def recent_decisions(self, limit):
        return [
            {"market": "crypto", "symbol": "ETHUSDT", "horizon": "short", "confidence": 80, "action": "ENTER"},
            {"market": "stock", "symbol": "MSFT", "horizon": "short", "confidence": 99, "action": "ENTER"},
        ]

    def assets(self):
        return [
            {"market": "crypto", "symbol": "BTCUSDT"},
            {"market": "crypto", "symbol": "ETHUSDT"},
            {"market": "stock", "symbol": "AAPL"},
            {"market": "stock", "symbol": "MSFT"},
        ]


def test_realtime_lite_watchlist_is_crypto_only_and_master_position_is_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")
    rt = RealtimeDB(str(tmp_path / "rt.sqlite3"))

    rows = build_realtime_watchlist(FakeRealtimeSource(), rt)

    assert _market_from_account_id("crypto") == "crypto"
    assert rows
    assert {row["market"] for row in rows} == {"crypto"}
    btc = next(row for row in rows if row["symbol"] == "BTCUSDT")
    assert btc["reason"] == "POSITION"
    assert btc["score"] == 1000.0


def test_realtime_db_is_not_persisted_in_crypto_lite_snapshot_policy():
    from pathlib import Path

    cloud = Path("cloud_start.sh").read_text(encoding="utf-8")
    rescue = Path("storage_rescue.py").read_text(encoding="utf-8")

    snapshot_line = next(line for line in cloud.splitlines() if line.startswith("export V6_SNAPSHOT_DBS="))
    assert "realtime_execution.sqlite3" not in snapshot_line
    default_block = rescue.split("DEFAULT_CRITICAL_DBS = {", 1)[1].split("}", 1)[0]
    assert "realtime_execution.sqlite3" not in default_block


def test_realtime_ticks_are_bounded_to_six_hours():
    from pathlib import Path

    worker = Path("realtime_worker.py").read_text(encoding="utf-8")
    assert "rt_db.prune_ticks(6)" in worker
