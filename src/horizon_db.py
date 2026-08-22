from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS horizon_candidates (
    sleeve_id TEXT PRIMARY KEY,
    base_candidate_id TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    horizon TEXT NOT NULL,
    params_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    research_grade REAL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    notes TEXT,
    UNIQUE(base_candidate_id, horizon)
);

CREATE TABLE IF NOT EXISTS horizon_state (
    sleeve_id TEXT PRIMARY KEY REFERENCES horizon_candidates(sleeve_id) ON DELETE CASCADE,
    cash REAL NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    entry_fill REAL,
    entry_cost_basis REAL NOT NULL DEFAULT 0,
    pending_target INTEGER NOT NULL DEFAULT 0,
    bars_in_position INTEGER NOT NULL DEFAULT 0,
    last_signal_bar TEXT,
    last_processed_bar TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS horizon_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve_id TEXT NOT NULL REFERENCES horizon_candidates(sleeve_id) ON DELETE CASCADE,
    bar_time TEXT NOT NULL,
    action TEXT NOT NULL,
    fill_price REAL NOT NULL,
    qty REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    reason TEXT,
    signal_bar TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(sleeve_id, bar_time, action, reason)
);

CREATE TABLE IF NOT EXISTS horizon_equity (
    sleeve_id TEXT NOT NULL REFERENCES horizon_candidates(sleeve_id) ON DELETE CASCADE,
    bar_time TEXT NOT NULL,
    cash REAL NOT NULL,
    qty REAL NOT NULL,
    close_price REAL NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY(sleeve_id, bar_time)
);

CREATE TABLE IF NOT EXISTS horizon_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    sleeves_checked INTEGER NOT NULL DEFAULT 0,
    bars_processed INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);
"""

class HorizonDB:
    def __init__(self, path: str | Path = "multi_horizon_validation.sqlite3"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def register_sleeve(self, row: dict):
        with self.connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO horizon_candidates
                (sleeve_id,base_candidate_id,market,symbol,strategy,horizon,params_json,
                 registered_at,initial_capital,research_grade,status,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["sleeve_id"], row["base_candidate_id"], row["market"], row["symbol"],
                    row["strategy"], row["horizon"],
                    json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
                    row["registered_at"], float(row["initial_capital"]),
                    row.get("research_grade"), row.get("status","ACTIVE"), row.get("notes"),
                ),
            )
            con.execute(
                """INSERT OR IGNORE INTO horizon_state
                (sleeve_id,cash,qty,entry_fill,entry_cost_basis,pending_target,bars_in_position,
                 last_signal_bar,last_processed_bar,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["sleeve_id"], float(row["initial_capital"]), 0.0, None, 0.0,
                    0, 0, None, None, row["registered_at"],
                ),
            )

    def sleeves(self, status: str | None = None):
        q = "SELECT * FROM horizon_candidates"
        args = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY symbol,horizon"
        with self.connect() as con:
            return [dict(r) for r in con.execute(q,args).fetchall()]

    def state(self, sleeve_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM horizon_state WHERE sleeve_id=?", (sleeve_id,)).fetchone()
            if row is None:
                raise KeyError(sleeve_id)
            return dict(row)

    def update_state(self, sleeve_id: str, state: dict):
        with self.connect() as con:
            con.execute(
                """UPDATE horizon_state SET
                cash=?,qty=?,entry_fill=?,entry_cost_basis=?,pending_target=?,bars_in_position=?,
                last_signal_bar=?,last_processed_bar=?,updated_at=? WHERE sleeve_id=?""",
                (
                    state["cash"],state["qty"],state.get("entry_fill"),
                    state.get("entry_cost_basis",0.0),int(state.get("pending_target",0)),
                    int(state.get("bars_in_position",0)),state.get("last_signal_bar"),
                    state.get("last_processed_bar"),state["updated_at"],sleeve_id,
                ),
            )

    def insert_trade(self, row: dict):
        with self.connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO horizon_trades
                (sleeve_id,bar_time,action,fill_price,qty,realized_pnl,reason,signal_bar,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    row["sleeve_id"],row["bar_time"],row["action"],row["fill_price"],row["qty"],
                    row.get("realized_pnl",0.0),row.get("reason"),row.get("signal_bar"),row["created_at"],
                ),
            )

    def upsert_equity(self, row: dict):
        with self.connect() as con:
            con.execute(
                """INSERT INTO horizon_equity(sleeve_id,bar_time,cash,qty,close_price,equity)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(sleeve_id,bar_time) DO UPDATE SET cash=excluded.cash,qty=excluded.qty,
                close_price=excluded.close_price,equity=excluded.equity""",
                (row["sleeve_id"],row["bar_time"],row["cash"],row["qty"],row["close_price"],row["equity"]),
            )

    def trades(self, sleeve_id: str | None = None):
        with self.connect() as con:
            if sleeve_id:
                rows = con.execute(
                    "SELECT * FROM horizon_trades WHERE sleeve_id=? ORDER BY bar_time,id", (sleeve_id,)
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM horizon_trades ORDER BY sleeve_id,bar_time,id").fetchall()
            return [dict(r) for r in rows]

    def equity(self, sleeve_id: str):
        with self.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM horizon_equity WHERE sleeve_id=? ORDER BY bar_time", (sleeve_id,)
            ).fetchall()]

    def start_run(self, started_at: str) -> int:
        with self.connect() as con:
            cur = con.execute("INSERT INTO horizon_runs(started_at,status) VALUES (?,?)", (started_at,"RUNNING"))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_at: str, status: str,
                   sleeves_checked: int, bars_processed: int, error_text: str | None = None):
        with self.connect() as con:
            con.execute(
                """UPDATE horizon_runs SET finished_at=?,status=?,sleeves_checked=?,bars_processed=?,error_text=?
                WHERE run_id=?""",
                (finished_at,status,sleeves_checked,bars_processed,error_text,run_id),
            )
