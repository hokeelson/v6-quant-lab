from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CryptoV2ShadowDB:
    """Independent forward-only ledger for Crypto V2.

    This DB never writes to simulation_lab.sqlite3 and never calls broker APIs.
    """

    def __init__(self, path: str = "crypto_v2_shadow.sqlite3", initial_equity: float = 100000.0):
        self.path = str(path)
        self.initial_equity = float(initial_equity)
        self._init()
        self.ensure_accounts()

    def _c(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init(self):
        with self._c() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
              horizon TEXT PRIMARY KEY,
              initial_equity REAL NOT NULL,
              cash REAL NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions(
              decision_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              horizon TEXT NOT NULL,
              bar_time TEXT NOT NULL,
              action TEXT NOT NULL,
              strategy TEXT NOT NULL,
              confidence REAL NOT NULL,
              regime TEXT NOT NULL,
              reason TEXT,
              features_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(symbol,horizon,bar_time)
            );
            CREATE TABLE IF NOT EXISTS orders(
              order_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              horizon TEXT NOT NULL,
              side TEXT NOT NULL,
              created_bar TEXT NOT NULL,
              requested_notional REAL NOT NULL,
              status TEXT NOT NULL,
              decision_id TEXT,
              filled_bar TEXT,
              fill_price REAL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions(
              symbol TEXT NOT NULL,
              horizon TEXT NOT NULL,
              qty REAL NOT NULL,
              avg_entry REAL NOT NULL,
              entry_bar TEXT NOT NULL,
              strategy TEXT NOT NULL,
              regime_entry TEXT NOT NULL,
              stop_price REAL NOT NULL,
              target_price REAL NOT NULL,
              max_holding_bars INTEGER NOT NULL,
              bars_held INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(symbol,horizon)
            );
            CREATE TABLE IF NOT EXISTS trades(
              trade_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              horizon TEXT NOT NULL,
              entry_bar TEXT NOT NULL,
              exit_bar TEXT NOT NULL,
              qty REAL NOT NULL,
              entry_price REAL NOT NULL,
              exit_price REAL NOT NULL,
              realized_pnl REAL NOT NULL,
              return_pct REAL NOT NULL,
              strategy TEXT NOT NULL,
              regime_entry TEXT NOT NULL,
              exit_reason TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS engine_state(
              symbol TEXT NOT NULL,
              horizon TEXT NOT NULL,
              last_processed_bar TEXT,
              PRIMARY KEY(symbol,horizon)
            );
            CREATE TABLE IF NOT EXISTS market_states(
              bar_time TEXT PRIMARY KEY,
              state TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """)

    def ensure_accounts(self):
        with self._c() as c:
            for horizon in ("short", "medium", "long"):
                c.execute(
                    "INSERT OR IGNORE INTO accounts(horizon,initial_equity,cash,created_at) VALUES(?,?,?,?)",
                    (horizon, self.initial_equity, self.initial_equity, now_iso()),
                )

    def account(self, horizon: str) -> dict:
        with self._c() as c:
            r = c.execute("SELECT * FROM accounts WHERE horizon=?", (horizon,)).fetchone()
            return dict(r) if r else {}

    def last_processed(self, symbol: str, horizon: str):
        with self._c() as c:
            r = c.execute(
                "SELECT last_processed_bar FROM engine_state WHERE symbol=? AND horizon=?",
                (symbol.upper(), horizon),
            ).fetchone()
            return r[0] if r else None

    def set_last_processed(self, symbol: str, horizon: str, bar_time: str):
        with self._c() as c:
            c.execute(
                "INSERT INTO engine_state(symbol,horizon,last_processed_bar) VALUES(?,?,?) "
                "ON CONFLICT(symbol,horizon) DO UPDATE SET last_processed_bar=excluded.last_processed_bar",
                (symbol.upper(), horizon, bar_time),
            )

    def add_market_state(self, bar_time: str, payload: dict):
        with self._c() as c:
            c.execute(
                "INSERT OR REPLACE INTO market_states(bar_time,state,payload_json,created_at) VALUES(?,?,?,?)",
                (bar_time, str(payload.get("state") or "UNKNOWN"), json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    def add_decision(self, symbol: str, horizon: str, bar_time: str, decision: dict, regime: dict, features: dict):
        did = uuid.uuid4().hex
        with self._c() as c:
            c.execute(
                """INSERT OR IGNORE INTO decisions(
                    decision_id,symbol,horizon,bar_time,action,strategy,confidence,regime,reason,features_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    did, symbol.upper(), horizon, bar_time,
                    str(decision.get("action") or "NO_TRADE"),
                    str(decision.get("strategy") or "NONE"),
                    float(decision.get("confidence") or 0.0),
                    str(regime.get("state") or "UNKNOWN"),
                    str(decision.get("reason") or ""),
                    json.dumps(features, ensure_ascii=False, sort_keys=True), now_iso(),
                ),
            )
            row = c.execute(
                "SELECT decision_id FROM decisions WHERE symbol=? AND horizon=? AND bar_time=?",
                (symbol.upper(), horizon, bar_time),
            ).fetchone()
            return row[0] if row else did

    def pending_order(self, symbol: str, horizon: str):
        with self._c() as c:
            r = c.execute(
                "SELECT * FROM orders WHERE symbol=? AND horizon=? AND status='PENDING' ORDER BY created_at DESC LIMIT 1",
                (symbol.upper(), horizon),
            ).fetchone()
            return dict(r) if r else None

    def position(self, symbol: str, horizon: str):
        with self._c() as c:
            r = c.execute(
                "SELECT * FROM positions WHERE symbol=? AND horizon=?",
                (symbol.upper(), horizon),
            ).fetchone()
            return dict(r) if r else None

    def add_buy_order(self, symbol: str, horizon: str, created_bar: str, requested_notional: float, decision_id: str):
        oid = uuid.uuid4().hex
        with self._c() as c:
            c.execute(
                "INSERT INTO orders(order_id,symbol,horizon,side,created_bar,requested_notional,status,decision_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (oid, symbol.upper(), horizon, "BUY", created_bar, float(requested_notional), "PENDING", decision_id, now_iso()),
            )
        return oid

    def fill_buy(self, order: dict, bar_time: str, price: float, fee_rate: float, decision: dict, regime: dict):
        horizon = str(order["horizon"])
        symbol = str(order["symbol"])
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            acct = c.execute("SELECT * FROM accounts WHERE horizon=?", (horizon,)).fetchone()
            if not acct:
                return False
            cash = float(acct["cash"])
            requested = min(float(order.get("requested_notional") or 0.0), cash / max(1.0 + fee_rate, 1e-9))
            if requested <= 0 or price <= 0:
                c.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=? AND status='PENDING'", (order["order_id"],))
                return False
            qty = requested / price
            cost = requested * (1.0 + fee_rate)
            cur = c.execute(
                "UPDATE orders SET status='FILLED',filled_bar=?,fill_price=? WHERE order_id=? AND status='PENDING'",
                (bar_time, float(price), order["order_id"]),
            )
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE accounts SET cash=? WHERE horizon=?", (cash - cost, horizon))
            stop = price * (1.0 - float(decision.get("stop_distance") or 0.03))
            target = price * (1.0 + float(decision.get("target_distance") or 0.06))
            c.execute(
                """INSERT OR REPLACE INTO positions(
                    symbol,horizon,qty,avg_entry,entry_bar,strategy,regime_entry,stop_price,target_price,max_holding_bars,bars_held
                ) VALUES(?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    symbol, horizon, qty, float(price), bar_time,
                    str(decision.get("strategy") or "UNKNOWN"), str(regime.get("state") or "UNKNOWN"),
                    stop, target, int(decision.get("max_holding_bars") or 8),
                ),
            )
            return True

    def close_position(self, symbol: str, horizon: str, bar_time: str, exit_price: float, fee_rate: float, reason: str):
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            p = c.execute(
                "SELECT * FROM positions WHERE symbol=? AND horizon=?",
                (symbol.upper(), horizon),
            ).fetchone()
            if not p:
                return False
            acct = c.execute("SELECT * FROM accounts WHERE horizon=?", (horizon,)).fetchone()
            qty = float(p["qty"])
            entry = float(p["avg_entry"])
            gross_exit = qty * float(exit_price)
            proceeds = gross_exit * (1.0 - fee_rate)
            entry_cost = qty * entry * (1.0 + fee_rate)
            pnl = proceeds - entry_cost
            ret = pnl / entry_cost if entry_cost > 0 else 0.0
            c.execute("UPDATE accounts SET cash=? WHERE horizon=?", (float(acct["cash"]) + proceeds, horizon))
            c.execute(
                """INSERT INTO trades(
                    trade_id,symbol,horizon,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,
                    strategy,regime_entry,exit_reason,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex, symbol.upper(), horizon, p["entry_bar"], bar_time, qty, entry,
                    float(exit_price), pnl, ret, p["strategy"], p["regime_entry"], reason, now_iso(),
                ),
            )
            c.execute("DELETE FROM positions WHERE symbol=? AND horizon=?", (symbol.upper(), horizon))
            return True

    def increment_holding(self, symbol: str, horizon: str):
        with self._c() as c:
            c.execute(
                "UPDATE positions SET bars_held=bars_held+1 WHERE symbol=? AND horizon=?",
                (symbol.upper(), horizon),
            )

    def equity(self, horizon: str, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        acct = self.account(horizon)
        value = float(acct.get("cash") or 0.0)
        with self._c() as c:
            for p in c.execute("SELECT * FROM positions WHERE horizon=?", (horizon,)).fetchall():
                px = float(marks.get(str(p["symbol"]), p["avg_entry"]))
                value += float(p["qty"]) * px
        return value

    def recent_decisions(self, limit: int = 100):
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM decisions ORDER BY bar_time DESC LIMIT ?", (int(limit),))]

    def recent_trades(self, limit: int = 100):
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM trades ORDER BY exit_bar DESC LIMIT ?", (int(limit),))]

    def positions(self):
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY horizon,symbol")]

    def summary(self) -> dict:
        out = {"accounts": [], "closed_trades": 0, "realized_pnl": 0.0, "wins": 0, "losses": 0}
        with self._c() as c:
            for horizon in ("short", "medium", "long"):
                acct = c.execute("SELECT * FROM accounts WHERE horizon=?", (horizon,)).fetchone()
                trades = c.execute("SELECT realized_pnl FROM trades WHERE horizon=?", (horizon,)).fetchall()
                pnls = [float(r[0]) for r in trades]
                positions = c.execute("SELECT COUNT(*) FROM positions WHERE horizon=?", (horizon,)).fetchone()[0]
                realized = sum(pnls)
                initial = float(acct["initial_equity"]) if acct else self.initial_equity
                cash = float(acct["cash"]) if acct else initial
                out["accounts"].append({
                    "horizon": horizon,
                    "initial_equity": initial,
                    "cash": cash,
                    "realized_pnl": realized,
                    "closed_trades": len(pnls),
                    "wins": sum(1 for x in pnls if x > 0),
                    "losses": sum(1 for x in pnls if x <= 0),
                    "open_positions": int(positions),
                })
                out["closed_trades"] += len(pnls)
                out["realized_pnl"] += realized
                out["wins"] += sum(1 for x in pnls if x > 0)
                out["losses"] += sum(1 for x in pnls if x <= 0)
        out["win_rate"] = out["wins"] / out["closed_trades"] if out["closed_trades"] else None
        return out
