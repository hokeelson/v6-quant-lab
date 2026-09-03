import json
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from src.entry_gate import finalize_entry
from src.execution_audit import execution_audit
from src.simulation_db import SimulationDB
from src.twstock_support import TaiwanSimulationDB, TaiwanSimulationLab


@pytest.fixture
def setup_lab(tmp_path):
    db = TaiwanSimulationDB(str(tmp_path / "simulation.sqlite3"))
    db.ensure_accounts()
    return db, TaiwanSimulationLab(db=db)


def buy(db, lab, market="stock", allowed=True):
    aid = market + "_short"
    did = db.add_decision({"account_id": aid, "market": market, "symbol": "TEST", "horizon": "short",
                           "bar_time": "2026-01-01T00:00:00+00:00", "action": "ENTER", "confidence": .8,
                           "strategy": "trend", "regime": "NORMAL", "atr_pct": .02, "stop_distance": .1,
                           "target_distance": .2, "risk_budget_pct": .01, "requested_notional": 1000,
                           "leverage": 1, "reason": "TEST"})
    oid = db.add_order({"account_id": aid, "symbol": "TEST", "side": "BUY", "created_bar": "2026-01-01T00:00:00+00:00",
                       "requested_notional": 1000, "qty": None, "reason": "MODEL_ENTER", "decision_id": did})
    sizing = finalize_entry({"original_notional": 1000, "adjusted_notional": 1000 if allowed else 0})
    target = "src.twstock_support" if market == "twstock" else "src.simulation_engine"
    with patch(target + ".active_entry_sizing", return_value=sizing):
        lab._execute_pending(aid, market, "TEST", pd.Timestamp("2026-01-02", tz="UTC"), pd.Series({"open": 100.0}))
    return aid, oid


@pytest.mark.parametrize("market", ["stock", "crypto", "twstock"])
@pytest.mark.parametrize("protective", [True, False])
def test_real_engine_roundtrip_exact_links(setup_lab, market, protective):
    db, lab = setup_lab
    aid, oid = buy(db, lab, market)
    assert db.position(aid, "TEST")["entry_order_id"] == oid
    report = execution_audit(db.path)
    assert report["lifecycle"]["summary"] == {"VALIDATED_OPEN": 1}
    # A normal no-exit position update must preserve its immutable entry link.
    lab._protect_position(aid, market, "TEST", pd.Timestamp("2026-01-02", tz="UTC"),
                          pd.Series({"open": 100, "high": 101, "low": 99, "close": 100}))
    assert db.position(aid, "TEST")["entry_order_id"] == oid
    if protective:
        lab._protect_position(aid, market, "TEST", pd.Timestamp("2026-01-03", tz="UTC"),
                              pd.Series({"open": 80, "high": 85, "low": 75, "close": 82}))
    else:
        db.add_order({"account_id": aid, "symbol": "TEST", "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00",
                      "requested_notional": 0, "qty": db.position(aid, "TEST")["qty"], "reason": "MODEL_EXIT", "decision_id": None})
        lab._execute_pending(aid, market, "TEST", pd.Timestamp("2026-01-03", tz="UTC"), pd.Series({"open": 110}))
    assert db.position(aid, "TEST") is None
    assert db.recent_trades()[0]["entry_order_id"] == oid
    report = execution_audit(db.path)
    assert report["lifecycle"]["summary"] == {"VALIDATED_CLOSED": 1}, report
    group = report["pnl_attribution"]["groups"]["by_account"][0]
    assert group["realized_pnl"] == db.recent_trades()[0]["realized_pnl"]


@pytest.mark.parametrize("market", ["stock", "crypto", "twstock"])
def test_blocked_order_proof_and_no_spend(setup_lab, market):
    db, lab = setup_lab
    aid, oid = buy(db, lab, market, False)
    assert db.account(aid)["cash"] == 100000
    assert db.position(aid, "TEST") is None
    report = execution_audit(db.path)
    assert report["lifecycle"]["summary"] == {"VALIDATED_BLOCKED": 1}
    # Corrupt only this disposable test DB to simulate the prohibited outcome.
    db.fill_order(oid, "2026-01-02T00:00:00+00:00", 100, 0, 0)
    report = execution_audit(db.path)
    assert report["status"] == "ERROR"
    assert "BLOCKED_BUT_FILLED" in report["lifecycle"]["entries"][0]["issues"]


def test_legacy_not_guessed_or_repaired(setup_lab):
    db, lab = setup_lab
    aid, oid = buy(db, lab)
    with db._c() as c:
        c.execute("UPDATE positions SET entry_order_id=NULL")
    report = execution_audit(db.path)
    assert report["lifecycle"]["summary"] == {"UNRESOLVED": 1}
    assert db.position(aid, "TEST")["entry_order_id"] is None


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE positions SET avg_entry=1", "ENTRY_LINK_VALUE_MISMATCH"),
    ("UPDATE decisions SET bar_time='2027-01-01T00:00:00Z'", "INVALID_DECISION_FILL_SEQUENCE"),
])
def test_broken_proof_never_validates(setup_lab, sql, reason):
    db, lab = setup_lab
    buy(db, lab)
    with db._c() as c:
        c.execute(sql)
    report = execution_audit(db.path)
    assert report["status"] == "ERROR"
    assert reason in report["lifecycle"]["entries"][0]["issues"]


def test_duplicate_gate_is_ambiguous(setup_lab):
    db, lab = setup_lab
    aid, _ = buy(db, lab)
    row = db.diagnostics()[0]
    db.add_diagnostic(aid, "TEST", "short", row["bar_time"], "RISK_SIZING", "duplicate", json.loads(row["payload_json"]))
    assert execution_audit(db.path)["lifecycle"]["summary"] == {"UNRESOLVED": 1}


def test_observer_missing_file_does_not_create_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    assert execution_audit(path)["error"] == "DATABASE_MISSING"
    assert not path.exists()


def test_observer_preserves_logical_database(setup_lab):
    db, lab = setup_lab
    buy(db, lab)
    with sqlite3.connect(db.path) as c:
        before = list(c.iterdump())
    execution_audit(db.path)
    with sqlite3.connect(db.path) as c:
        assert before == list(c.iterdump())


def test_schema_v1_migration_preserves_cash_and_legacy_rows(setup_lab):
    db, lab = setup_lab
    aid, _ = buy(db, lab)
    cash = db.account(aid)["cash"]
    with db._c() as c:
        for table in ("positions", "trades"):
            c.execute(f"DROP INDEX ix_{table}_entry_order")
            c.execute(f"ALTER TABLE {table} DROP COLUMN entry_order_id")
        c.execute("PRAGMA user_version=1")
    migrated = SimulationDB(db.path)
    assert migrated.account(aid)["cash"] == cash
    assert migrated.position(aid, "TEST")["entry_order_id"] is None
    assert execution_audit(db.path)["lifecycle"]["summary"] == {"UNRESOLVED": 1}


def test_failed_atomic_close_rolls_back_link_and_cash(setup_lab):
    db, lab = setup_lab
    aid, oid = buy(db, lab)
    cash = db.account(aid)["cash"]
    with db._c() as c:
        c.execute("CREATE TRIGGER fail_close BEFORE DELETE ON positions BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        lab._protect_position(aid, "stock", "TEST", pd.Timestamp("2026-01-03", tz="UTC"),
                              pd.Series({"open": 80, "high": 85, "low": 75, "close": 82}))
    assert db.account(aid)["cash"] == cash
    assert db.position(aid, "TEST")["entry_order_id"] == oid
    assert not db.recent_trades()


def test_bounded_sql_timeout_reports_error_not_pass(setup_lab):
    db, lab = setup_lab
    buy(db, lab)
    with patch("src.execution_audit.time.monotonic", side_effect=[0, 100]):
        assert execution_audit(db.path)["status"] == "ERROR"


def test_margin_close_keeps_entry_id(setup_lab):
    db, lab = setup_lab
    aid, oid = buy(db, lab)
    db.set_cash(aid, -900)
    db.set_mark(aid, "TEST", "2026-01-03T00:00:00Z", 100)
    assert lab._margin_check(aid, "stock", "short", pd.Timestamp("2026-01-03", tz="UTC"))
    report = execution_audit(db.path)
    assert report["lifecycle"]["summary"] == {"VALIDATED_CLOSED": 1}
    assert report["lifecycle"]["entries"][0]["exit_reason"] == "MARGIN_LIQUIDATION"


def test_concurrent_initializers_serialize_migration(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / "concurrent.sqlite3")
    # Exercise the deployment case: several processes open the same restored
    # schema-1 database, not simultaneous first-ever creation of a WAL file.
    db = SimulationDB(path)
    with db._c() as con:
        for table in ("positions", "trades"):
            con.execute(f"DROP INDEX ix_{table}_entry_order")
            con.execute(f"ALTER TABLE {table} DROP COLUMN entry_order_id")
        con.execute("PRAGMA user_version=1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: SimulationDB(path), range(8)))
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [r[1] for r in con.execute("PRAGMA table_info(trades)")].count("entry_order_id") == 1


def test_bad_trade_pnl_is_not_validated(setup_lab):
    db, lab = setup_lab
    aid, _ = buy(db, lab)
    lab._protect_position(aid, "stock", "TEST", pd.Timestamp("2026-01-03", tz="UTC"),
                          pd.Series({"open": 80, "high": 85, "low": 75, "close": 82}))
    with db._c() as con:
        con.execute("UPDATE trades SET realized_pnl=999")
    assert "PNL_MISMATCH" in execution_audit(db.path)["lifecycle"]["entries"][0]["issues"]


def test_public_export_and_dashboard_wiring():
    from pathlib import Path
    assert '"execution_audit": execution_audit(sim_path)' in Path("trial_ledger_worker.py").read_text()
    assert "render_execution_audit(st, research)" in Path("dashboard_v9.py").read_text()
    sync = Path(".github/workflows/sync-runtime-snapshots.yml").read_text()
    for name in ("current_snapshot_db", "current_snapshot_phase", "current_snapshot_progress"):
        assert name in sync
