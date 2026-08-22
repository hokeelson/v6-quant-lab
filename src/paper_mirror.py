from __future__ import annotations
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .horizon_db import HorizonDB
from .paper import AlpacaPaperBroker

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS mirror_slots (
    sleeve_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    horizon TEXT NOT NULL,
    notional REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_events (
    horizon_trade_id INTEGER PRIMARY KEY,
    sleeve_id TEXT NOT NULL,
    action TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    paper_order_id TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

class PaperMirrorDB:
    def __init__(self, path: str | Path = "paper_mirror.sqlite3"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con: con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path); con.row_factory = sqlite3.Row
        try:
            yield con; con.commit()
        finally:
            con.close()

    def add_slot(self, sleeve_id: str, symbol: str, horizon: str, notional: float):
        if notional <= 0: raise ValueError("notional must be > 0")
        with self.connect() as con:
            con.execute(
                """INSERT INTO mirror_slots(sleeve_id,symbol,horizon,notional,enabled,created_at)
                VALUES (?,?,?,?,1,?)
                ON CONFLICT(sleeve_id) DO UPDATE SET notional=excluded.notional,enabled=1""",
                (sleeve_id,symbol.upper(),horizon,float(notional),utc_now_iso()),
            )

    def disable_slot(self, sleeve_id: str):
        with self.connect() as con:
            con.execute("UPDATE mirror_slots SET enabled=0 WHERE sleeve_id=?",(sleeve_id,))

    def slots(self, enabled_only: bool = False):
        q = "SELECT * FROM mirror_slots" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY symbol"
        with self.connect() as con: return [dict(r) for r in con.execute(q).fetchall()]

    def event(self, trade_id: int):
        with self.connect() as con:
            r = con.execute("SELECT * FROM mirror_events WHERE horizon_trade_id=?",(int(trade_id),)).fetchone()
            return dict(r) if r else None

    def reserve_event(self, trade_id: int, sleeve_id: str, action: str, client_order_id: str) -> bool:
        now = utc_now_iso()
        with self.connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO mirror_events
                (horizon_trade_id,sleeve_id,action,client_order_id,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""",
                (int(trade_id),sleeve_id,action,client_order_id,"PENDING",now,now),
            )
            return cur.rowcount == 1

    def finish_event(self, trade_id: int, status: str, paper_order_id: str | None = None, error_text: str | None = None):
        with self.connect() as con:
            con.execute(
                """UPDATE mirror_events SET status=?,paper_order_id=?,error_text=?,updated_at=?
                WHERE horizon_trade_id=?""",
                (status,paper_order_id,error_text,utc_now_iso(),int(trade_id)),
            )

    def events(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM mirror_events ORDER BY horizon_trade_id DESC").fetchall()]

def client_id_for(trade_id: int, sleeve_id: str, action: str) -> str:
    h = hashlib.sha256(f"{trade_id}|{sleeve_id}|{action}".encode()).hexdigest()[:20]
    return f"v6h-{action.lower()}-{h}"

class AlpacaPaperMirror:
    """
    Mirrors selected STOCK horizon sleeve trade events to an Alpaca PAPER account.

    One mirror slot per symbol is enforced because Alpaca uses one net position per
    symbol; multiple strategies on the same symbol cannot be cleanly attributed.
    """
    def __init__(self, horizon_db: HorizonDB, mirror_db: PaperMirrorDB, broker: AlpacaPaperBroker | None = None):
        self.horizon_db = horizon_db; self.mirror_db = mirror_db; self.broker = broker or AlpacaPaperBroker()

    def run_once(self) -> dict:
        if not self.broker.allow:
            return {"status":"DISABLED","events_checked":0,"orders_submitted":0,"errors":[]}
        checked = submitted = 0; errors = []
        sleeves = {s["sleeve_id"]:s for s in self.horizon_db.sleeves()}
        for slot in self.mirror_db.slots(enabled_only=True):
            sleeve = sleeves.get(slot["sleeve_id"])
            if not sleeve or sleeve["market"] != "stock":
                continue
            for trade in self.horizon_db.trades(slot["sleeve_id"]):
                checked += 1
                if self.mirror_db.event(trade["id"]):
                    continue
                cid = client_id_for(trade["id"],slot["sleeve_id"],trade["action"])
                if not self.mirror_db.reserve_event(trade["id"],slot["sleeve_id"],trade["action"],cid):
                    continue
                try:
                    if trade["action"] == "BUY":
                        resp = self.broker.submit_market_notional_buy(slot["symbol"],slot["notional"],cid)
                    else:
                        resp = self.broker.close_position(slot["symbol"])
                    order_id = resp.get("id") if isinstance(resp,dict) else None
                    status = "SUBMITTED" if not (isinstance(resp,dict) and resp.get("status") == "no_position") else "SKIPPED_NO_POSITION"
                    self.mirror_db.finish_event(trade["id"],status,order_id,None); submitted += int(status == "SUBMITTED")
                except Exception as e:
                    # PENDING is intentionally not automatically retried after an ambiguous request.
                    # This avoids accidental duplicate paper orders after network timeouts.
                    self.mirror_db.finish_event(trade["id"],"ERROR",None,f"{type(e).__name__}: {e}")
                    errors.append(f"trade {trade['id']}: {type(e).__name__}: {e}")
        return {"status":"OK" if not errors else "PARTIAL","events_checked":checked,"orders_submitted":submitted,"errors":errors}
