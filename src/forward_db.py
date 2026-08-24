from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Do not change SQLite journal/synchronous modes at runtime on Railway persistent
# volumes. Deployment hand-offs can leave filesystem state where journal-mode
# transitions fail with `disk I/O error`. Keep the existing DB format untouched.
SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    params_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    research_grade REAL,
    evidence_coverage REAL,
    source_stage TEXT NOT NULL DEFAULT 'stage3',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS forward_state (
    candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    cash REAL NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    entry_fill REAL,
    entry_cost_basis REAL NOT NULL DEFAULT 0,
    pending_target INTEGER NOT NULL DEFAULT 0,
    last_signal_bar TEXT,
    last_processed_bar TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    bar_time TEXT NOT NULL,
    action TEXT NOT NULL,
    fill_price REAL NOT NULL,
    qty REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    reason TEXT,
    signal_bar TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, bar_time, action, reason)
);

CREATE TABLE IF NOT EXISTS forward_equity (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    bar_time TEXT NOT NULL,
    cash REAL NOT NULL,
    qty REAL NOT NULL,
    close_price REAL NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY(candidate_id, bar_time)
);

CREATE TABLE IF NOT EXISTS forward_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    candidates_checked INTEGER NOT NULL DEFAULT 0,
    bars_processed INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);
"""

class ForwardDB:
    def __init__(self, path: str | Path = "forward_validation.sqlite3"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            # Avoid PRAGMA journal_mode / synchronous here. They can require
            # sidecar creation or journal transitions and are the failing I/O
            # operation on the mounted Railway volume.
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA foreign_keys=ON")
            yield con
            con.commit()
        finally:
            con.close()

    def register_candidate(self, row: dict):
        with self.connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO candidates
                (candidate_id, market, symbol, strategy, params_json, registered_at,
                 initial_capital, research_grade, evidence_coverage, source_stage, status, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["candidate_id"], row["market"], row["symbol"], row["strategy"],
                    json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
                    row["registered_at"], float(row["initial_capital"]),
                    row.get("research_grade"), row.get("evidence_coverage"),
                    row.get("source_stage","stage3"), row.get("status","ACTIVE"),
                    row.get("notes"),
                ),
            )
            con.execute(
                """INSERT OR IGNORE INTO forward_state
                (candidate_id,cash,qty,entry_fill,entry_cost_basis,pending_target,
                 last_signal_bar,last_processed_bar,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    row["candidate_id"], float(row["initial_capital"]), 0.0, None, 0.0,
                    0, None, None, row["registered_at"]
                ),
            )

    def candidates(self, status: str | None = None):
        q = "SELECT * FROM candidates"
        args = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY registered_at, candidate_id"
        with self.connect() as con:
            return [dict(r) for r in con.execute(q, args).fetchall()]

    def state(self, candidate_id: str) -> dict:
        with self.connect() as con:
            r = con.execute("SELECT * FROM forward_state WHERE candidate_id=?", (candidate_id,)).fetchone()
            if r is None:
                raise KeyError(candidate_id)
            return dict(r)

    def upsert_state(self, candidate_id: str, state: dict):
        with self.connect() as con:
            con.execute(
                """UPDATE forward_state SET
                cash=?, qty=?, entry_fill=?, entry_cost_basis=?, pending_target=?,
                last_signal_bar=?, last_processed_bar=?, updated_at=?
                WHERE candidate_id=?""",
                (
                    state["cash"], state["qty"], state.get("entry_fill"),
                    state.get("entry_cost_basis",0.0), int(state.get("pending_target",0)),
                    state.get("last_signal_bar"), state.get("last_processed_bar"),
                    state["updated_at"], candidate_id
                )
            )

    def insert_trade(self, t: dict):
        with self.connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO forward_trades
                (candidate_id,bar_time,action,fill_price,qty,realized_pnl,reason,signal_bar,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    t["candidate_id"], t["bar_time"], t["action"], t["fill_price"],
                    t["qty"], t.get("realized_pnl",0.0), t.get("reason"),
                    t.get("signal_bar"), t["created_at"]
                )
            )

    def upsert_equity(self, e: dict):
        with self.connect() as con:
            con.execute(
                """INSERT INTO forward_equity
                (candidate_id,bar_time,cash,qty,close_price,equity)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(candidate_id,bar_time) DO UPDATE SET
                cash=excluded.cash, qty=excluded.qty, close_price=excluded.close_price,
                equity=excluded.equity""",
                (
                    e["candidate_id"], e["bar_time"], e["cash"], e["qty"],
                    e["close_price"], e["equity"]
                )
            )

    def trades(self, candidate_id: str | None = None):
        with self.connect() as con:
            if candidate_id:
                rows = con.execute(
                    "SELECT * FROM forward_trades WHERE candidate_id=? ORDER BY bar_time,id",
                    (candidate_id,)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM forward_trades ORDER BY candidate_id,bar_time,id"
                ).fetchall()
            return [dict(r) for r in rows]

    def equity(self, candidate_id: str):
        with self.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM forward_equity WHERE candidate_id=? ORDER BY bar_time",
                (candidate_id,)
            ).fetchall()]

    def set_status(self, candidate_id: str, status: str, notes: str | None = None):
        with self.connect() as con:
            con.execute(
                "UPDATE candidates SET status=?, notes=COALESCE(?,notes) WHERE candidate_id=?",
                (status, notes, candidate_id)
            )

    def start_run(self, started_at: str) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO forward_runs(started_at,status) VALUES (?,?)",
                (started_at,"RUNNING")
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_at: str, status: str,
                   candidates_checked: int, bars_processed: int, error_text: str | None = None):
        with self.connect() as con:
            con.execute(
                """UPDATE forward_runs SET finished_at=?,status=?,candidates_checked=?,
                bars_processed=?,error_text=? WHERE run_id=?""",
                (finished_at,status,candidates_checked,bars_processed,error_text,run_id)
            )
