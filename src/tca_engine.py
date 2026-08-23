from __future__ import annotations

import math
import sqlite3
import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value):
    if not value:
        return None
    try:
        x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        return x.astimezone(timezone.utc)
    except Exception:
        return None


def _finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _bps(value):
    return None if value is None else float(value) * 10000.0


TRIGGERS = {
    "ENTRY_CONFIRM": ("BUY", "確認進場"),
    "STOP_TOUCH": ("SELL", "觸及停損"),
    "TARGET_TOUCH": ("SELL", "觸及目標"),
}


class TCAStore:
    """Realtime shadow transaction-cost-analysis store.

    Events are created only when a realtime signal changes into one of the trigger
    states above. The shadow executable price is best ask for BUY and best bid for
    SELL, falling back to last trade/mid when a quote side is unavailable.
    No broker order API is used.
    """

    def __init__(self, realtime_db):
        self.rt = realtime_db
        self._init()

    def _c(self):
        c = sqlite3.connect(self.rt.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init(self):
        with self._c() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS tca_signal_state(
              market TEXT NOT NULL,
              symbol TEXT NOT NULL,
              last_signal TEXT,
              last_signal_ts TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(market,symbol)
            );
            CREATE TABLE IF NOT EXISTS tca_events(
              event_id TEXT PRIMARY KEY,
              market TEXT NOT NULL,
              symbol TEXT NOT NULL,
              trigger_signal TEXT NOT NULL,
              side TEXT NOT NULL,
              event_ts TEXT NOT NULL,
              signal_price REAL NOT NULL,
              bid REAL,
              ask REAL,
              mid REAL,
              spread_bps REAL,
              shadow_fill_price REAL NOT NULL,
              execution_cost_bps REAL,
              source TEXT,
              confidence REAL,
              detail TEXT,
              price_5s REAL,
              ts_5s TEXT,
              markout_5s_bps REAL,
              price_30s REAL,
              ts_30s TEXT,
              markout_30s_bps REAL,
              price_60s REAL,
              ts_60s TEXT,
              markout_60s_bps REAL,
              status TEXT NOT NULL DEFAULT 'PENDING',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tca_events_symbol_ts
              ON tca_events(market,symbol,event_ts);
            CREATE INDEX IF NOT EXISTS idx_tca_events_status
              ON tca_events(status,event_ts);
            """)

    @staticmethod
    def _quote_fresh(quote: dict, max_age_seconds: float = 15.0) -> bool:
        qts = _dt(quote.get("ts"))
        if qts is None:
            return False
        return 0 <= (datetime.now(timezone.utc) - qts).total_seconds() <= max_age_seconds

    @staticmethod
    def _markout(side: str, fill: float, future: float):
        if fill <= 0 or future <= 0:
            return None
        if side == "BUY":
            return (future / fill - 1.0) * 10000.0
        return (fill / future - 1.0) * 10000.0

    def _update_followups(self, market: str, symbol: str, quote: dict):
        price = _finite(quote.get("price"))
        qts = _dt(quote.get("ts"))
        if price is None or price <= 0 or qts is None:
            return 0
        changed = 0
        with self._c() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM tca_events WHERE market=? AND symbol=? AND status!='COMPLETE' ORDER BY event_ts",
                (market, symbol.upper()),
            )]
            for e in rows:
                ets = _dt(e.get("event_ts"))
                if ets is None or qts < ets:
                    continue
                age = (qts - ets).total_seconds()
                updates = {}
                for sec, pcol, tcol, mcol in (
                    (5, "price_5s", "ts_5s", "markout_5s_bps"),
                    (30, "price_30s", "ts_30s", "markout_30s_bps"),
                    (60, "price_60s", "ts_60s", "markout_60s_bps"),
                ):
                    if age >= sec and e.get(pcol) is None:
                        updates[pcol] = price
                        updates[tcol] = quote.get("ts")
                        updates[mcol] = self._markout(str(e.get("side")), float(e.get("shadow_fill_price")), price)
                if not updates:
                    continue
                status = "COMPLETE" if (updates.get("price_60s") is not None or e.get("price_60s") is not None) else "PARTIAL"
                sets = [f"{k}=?" for k in updates]
                vals = list(updates.values())
                sets.extend(["status=?", "updated_at=?"])
                vals.extend([status, _now_iso(), e["event_id"]])
                c.execute(f"UPDATE tca_events SET {', '.join(sets)} WHERE event_id=?", vals)
                changed += 1
        return changed

    def observe(self, market: str, symbol: str, signal: dict | None, quote: dict | None,
                max_quote_age_seconds: float = 15.0):
        symbol = str(symbol or "").upper()
        if not market or not symbol or not quote:
            return {"created": False, "updated": 0}

        updated = self._update_followups(market, symbol, quote)
        if not signal:
            return {"created": False, "updated": updated}

        current = str(signal.get("signal") or "").upper()
        signal_ts = str(signal.get("ts") or quote.get("ts") or _now_iso())
        with self._c() as c:
            prev = c.execute(
                "SELECT last_signal FROM tca_signal_state WHERE market=? AND symbol=?",
                (market, symbol),
            ).fetchone()
            previous = str(prev["last_signal"] or "") if prev else ""
            c.execute("""
              INSERT INTO tca_signal_state(market,symbol,last_signal,last_signal_ts,updated_at)
              VALUES(?,?,?,?,?)
              ON CONFLICT(market,symbol) DO UPDATE SET
                last_signal=excluded.last_signal,last_signal_ts=excluded.last_signal_ts,updated_at=excluded.updated_at
            """, (market, symbol, current, signal_ts, _now_iso()))

        if current == previous or current not in TRIGGERS:
            return {"created": False, "updated": updated}
        if not self._quote_fresh(quote, max_quote_age_seconds):
            return {"created": False, "updated": updated, "reason": "stale_quote"}

        side, _ = TRIGGERS[current]
        price = _finite(quote.get("price"))
        bid = _finite(quote.get("bid"))
        ask = _finite(quote.get("ask"))
        if price is None:
            if bid is not None and ask is not None:
                price = (bid + ask) / 2.0
            else:
                price = bid if bid is not None else ask
        if price is None or price <= 0:
            return {"created": False, "updated": updated, "reason": "no_price"}

        mid = (bid + ask) / 2.0 if bid is not None and ask is not None and bid > 0 and ask >= bid else None
        spread_bps = ((ask - bid) / mid * 10000.0) if mid else None
        if side == "BUY":
            fill = ask if ask is not None and ask > 0 else price
            cost = (fill / price - 1.0) * 10000.0
        else:
            fill = bid if bid is not None and bid > 0 else price
            cost = (price / fill - 1.0) * 10000.0 if fill > 0 else None

        eid = uuid.uuid4().hex
        now = _now_iso()
        with self._c() as c:
            c.execute("""
              INSERT INTO tca_events(
                event_id,market,symbol,trigger_signal,side,event_ts,signal_price,bid,ask,mid,spread_bps,
                shadow_fill_price,execution_cost_bps,source,confidence,detail,status,created_at,updated_at
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                eid, market, symbol, current, side, str(quote.get("ts") or now), price, bid, ask, mid,
                spread_bps, fill, cost, str(quote.get("source") or "STREAM"),
                _finite(signal.get("confidence")), str(signal.get("detail") or ""),
                "PENDING", now, now,
            ))
        return {"created": True, "event_id": eid, "updated": updated}

    def events(self, limit: int = 200):
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM tca_events ORDER BY event_ts DESC LIMIT ?", (int(limit),)
            )]

    def summary(self, limit: int = 500):
        rows = self.events(limit)
        if not rows:
            return {
                "samples": 0, "complete_60s": 0, "avg_execution_cost_bps": None,
                "avg_spread_bps": None, "avg_markout_5s_bps": None,
                "avg_markout_30s_bps": None, "avg_markout_60s_bps": None,
                "positive_60s_rate": None,
            }

        def avg(key):
            vals = [_finite(r.get(key)) for r in rows]
            vals = [x for x in vals if x is not None]
            return sum(vals) / len(vals) if vals else None

        m60 = [_finite(r.get("markout_60s_bps")) for r in rows]
        m60 = [x for x in m60 if x is not None]
        return {
            "samples": len(rows),
            "complete_60s": len(m60),
            "avg_execution_cost_bps": avg("execution_cost_bps"),
            "avg_spread_bps": avg("spread_bps"),
            "avg_markout_5s_bps": avg("markout_5s_bps"),
            "avg_markout_30s_bps": avg("markout_30s_bps"),
            "avg_markout_60s_bps": avg("markout_60s_bps"),
            "positive_60s_rate": (sum(1 for x in m60 if x > 0) / len(m60)) if m60 else None,
        }
