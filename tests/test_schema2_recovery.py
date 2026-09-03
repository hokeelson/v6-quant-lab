"""Also run against the pinned PR22 engine with ONLY the schema-2 DB adapter.

All databases are disposable test files. No live backup is restored or modified.
"""
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pandas as pd
import pytest

from src.entry_gate import finalize_entry
from src.simulation_db import SimulationDB
from src.twstock_support import TaiwanSimulationDB, TaiwanSimulationLab


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("V6_PERSISTENT_DATA_DIR", raising=False)
    db = TaiwanSimulationDB(str(tmp_path / "ledger.sqlite3"))
    db.ensure_accounts()
    return db


def dump(path):
    with closing(sqlite3.connect(str(path))) as c:
        return list(c.iterdump()), c.execute("PRAGMA user_version").fetchone()[0]


def enter(db, market, symbol, allowed=True):
    aid = market + "_short"
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "BUY",
                        "created_bar": "2026-01-01T00:00:00+00:00",
                        "requested_notional": 1000, "qty": None,
                        "reason": "MODEL_ENTER", "decision_id": None})
    module = "src.twstock_support" if market == "twstock" else "src.simulation_engine"
    sizing = finalize_entry({"original_notional": 1000,
                             "adjusted_notional": 1000 if allowed else 0})
    with patch(module + ".active_entry_sizing", return_value=sizing):
        TaiwanSimulationLab(db=db)._execute_pending(
            aid, market, symbol, pd.Timestamp("2026-01-02", tz="UTC"),
            pd.Series({"open": 100.0}))
    return aid, oid


@pytest.mark.parametrize("market,exit_kind", [
    (market, kind) for market in ("stock", "crypto", "twstock")
    for kind in ("model", "protective", "margin")
    if (market, kind) != ("twstock", "margin")
])
def test_recovery_engine_preserves_rows_and_continues_linked_trading(ledger, market, exit_kind):
    aid, oid = enter(ledger, market, "EXISTING")
    before = dump(ledger.path)
    # Restart on the SAME schema-2 ledger, never on a pre-upgrade backup.
    db = TaiwanSimulationDB(ledger.path)
    assert dump(db.path) == before
    assert before[1] == 2
    assert len(db.accounts()) == 9
    assert db.position(aid, "EXISTING")["entry_order_id"] == oid
    lab = TaiwanSimulationLab(db=db)
    day = pd.Timestamp("2026-01-03", tz="UTC")
    if exit_kind == "model":
        sell = db.add_order({"account_id": aid, "symbol": "EXISTING", "side": "SELL",
                             "created_bar": "2026-01-02T00:00:00+00:00",
                             "requested_notional": 0, "qty": db.position(aid, "EXISTING")["qty"],
                             "reason": "MODEL_EXIT", "decision_id": None})
        lab._execute_pending(aid, market, "EXISTING", day, pd.Series({"open": 110.0}))
    elif exit_kind == "protective":
        lab._protect_position(aid, market, "EXISTING", day,
                              pd.Series({"open": 1., "high": 2., "low": .5, "close": 1.}))
    else:
        db.set_cash(aid, -900)
        db.set_mark(aid, "EXISTING", day.isoformat(), 100)
        assert lab._margin_check(aid, market, "short", day)
    assert db.position(aid, "EXISTING") is None
    trade = db.recent_trades()[0]
    assert trade["entry_order_id"] == oid
    if exit_kind == "model":
        assert trade["exit_order_id"] == sell
    # A second restart must preserve all closed trades, cash and every table.
    after_close = dump(db.path)
    db = TaiwanSimulationDB(db.path)
    assert dump(db.path) == after_close
    cash = db.account(aid)["cash"]
    _, blocked = enter(db, market, "BLOCKED", allowed=False)
    assert db.position(aid, "BLOCKED") is None
    with closing(db._c()) as c:
        assert c.execute("SELECT status FROM orders WHERE order_id=?", (blocked,)).fetchone()[0] != "FILLED"
    assert db.account(aid)["cash"] == cash
    assert db.recent_trades()[0] == trade


def test_taiwan_cash_only_margin_check_remains_noop(ledger):
    aid, _ = enter(ledger, "twstock", "CASH_ONLY")
    before = dump(ledger.path)
    assert not TaiwanSimulationLab(db=ledger)._margin_check(
        aid, "twstock", "short", pd.Timestamp("2026-01-03", tz="UTC"))
    assert dump(ledger.path) == before


def test_current_wal_backup_preserves_new_trades_not_old_snapshot(ledger, tmp_path):
    old = dump(ledger.path)
    # Keep WAL connection alive; a raw main-file copy would not be sufficient.
    with closing(ledger._c()) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        enter(ledger, "stock", "NEW_AFTER_DEPLOY")
        expected = dump(ledger.path)
        assert expected != old
        saved = tmp_path / "current-recovery.sqlite3"
        with closing(sqlite3.connect(f"file:{ledger.path}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(saved)) as dest:
                source.backup(dest)
                assert dest.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert dump(saved) == expected
        recovered = TaiwanSimulationDB(str(saved))
        assert dump(recovered.path) == expected
        assert recovered.position("stock_short", "NEW_AFTER_DEPLOY")["entry_order_id"]
        assert dump(ledger.path) == expected


def test_interrupted_upgrade_is_atomic_and_retry_preserves_data(ledger):
    enter(ledger, "stock", "LEGACY")
    with closing(ledger._c()) as c, c:
        for table in ("positions", "trades"):
            c.execute(f"DROP INDEX ix_{table}_entry_order")
            c.execute(f"ALTER TABLE {table} DROP COLUMN entry_order_id")
        c.execute("PRAGMA user_version=1")
    before = dump(ledger.path)
    # Fail after the first table ALTER, before the second. Roll back the DDL too.
    with closing(ledger._c()) as c:
        c.set_authorizer(lambda op, a, b, *_: sqlite3.SQLITE_DENY
                         if op == sqlite3.SQLITE_ALTER_TABLE and b == "trades"
                         else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            with c:
                ledger._migrate(c)
    assert dump(ledger.path) == before
    cash = ledger.account("stock_short")["cash"]
    upgraded = TaiwanSimulationDB(ledger.path)
    assert upgraded.account("stock_short")["cash"] == cash
    assert upgraded.position("stock_short", "LEGACY")["entry_order_id"] is None
    assert dump(upgraded.path)[1] == 2


def test_recovery_adapter_rejects_future_schema(ledger):
    with closing(ledger._c()) as c, c:
        c.execute("PRAGMA user_version=3")
    before = dump(ledger.path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        SimulationDB(ledger.path)
    assert dump(ledger.path) == before
