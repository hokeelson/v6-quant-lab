from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from .paths import db_path


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _cap(market: str) -> int:
    defaults = {"crypto": 20, "stock": 20, "twstock": 10}
    env = {"crypto": "V6_REALTIME_CRYPTO", "stock": "V6_REALTIME_STOCK", "twstock": "V6_REALTIME_TWSTOCK"}
    try:
        return max(1, int(os.getenv(env[market], defaults[market])))
    except Exception:
        return defaults[market]


class RealtimeDB:
    def __init__(self, path: str | None = None):
        self.path = path or db_path("realtime_execution.sqlite3")
        self._init()

    def _c(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init(self):
        with self._c() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist(
              market TEXT NOT NULL,
              symbol TEXT NOT NULL,
              score REAL NOT NULL DEFAULT 0,
              reason TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(market,symbol)
            );
            CREATE TABLE IF NOT EXISTS quotes(
              market TEXT NOT NULL,
              symbol TEXT NOT NULL,
              ts TEXT NOT NULL,
              price REAL,
              bid REAL,
              ask REAL,
              spread_bps REAL,
              source TEXT,
              PRIMARY KEY(market,symbol)
            );
            CREATE TABLE IF NOT EXISTS signals(
              market TEXT NOT NULL,
              symbol TEXT NOT NULL,
              ts TEXT NOT NULL,
              signal TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0,
              detail TEXT,
              confidence REAL,
              PRIMARY KEY(market,symbol)
            );
            CREATE TABLE IF NOT EXISTS ticks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market TEXT NOT NULL,
              symbol TEXT NOT NULL,
              ts TEXT NOT NULL,
              price REAL NOT NULL,
              bid REAL,
              ask REAL,
              source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(market,symbol,ts);
            """)

    def set_watchlist(self, rows):
        rows = list(rows or [])
        with self._c() as c:
            c.execute("DELETE FROM watchlist")
            for r in rows:
                c.execute(
                    "INSERT INTO watchlist(market,symbol,score,reason,updated_at) VALUES(?,?,?,?,?)",
                    (str(r["market"]), str(r["symbol"]).upper(), float(r.get("score", 0) or 0),
                     str(r.get("reason", "watch")), now_iso()),
                )

    def watchlist(self, market=None):
        with self._c() as c:
            if market:
                q = c.execute("SELECT * FROM watchlist WHERE market=? ORDER BY score DESC,symbol", (market,))
            else:
                q = c.execute("SELECT * FROM watchlist ORDER BY market,score DESC,symbol")
            return [dict(x) for x in q]

    def upsert_quote(self, market, symbol, price=None, bid=None, ask=None, source="STREAM", ts=None):
        symbol = str(symbol).upper()
        ts = ts or now_iso()
        vals = [price, bid, ask]
        vals = [float(v) if v is not None else None for v in vals]
        price, bid, ask = vals
        if price is None:
            if bid is not None and ask is not None:
                price = (bid + ask) / 2.0
            elif bid is not None:
                price = bid
            elif ask is not None:
                price = ask
        spread = None
        mid = ((bid + ask) / 2.0) if bid is not None and ask is not None and (bid + ask) > 0 else None
        if mid and ask >= bid:
            spread = (ask - bid) / mid * 10000.0
        with self._c() as c:
            old = c.execute("SELECT price,bid,ask FROM quotes WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            if old:
                if price is None: price = old["price"]
                if bid is None: bid = old["bid"]
                if ask is None: ask = old["ask"]
                mid = ((bid + ask) / 2.0) if bid is not None and ask is not None and (bid + ask) > 0 else None
                spread = (ask - bid) / mid * 10000.0 if mid and ask >= bid else None
            c.execute("""
              INSERT INTO quotes(market,symbol,ts,price,bid,ask,spread_bps,source)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(market,symbol) DO UPDATE SET
                ts=excluded.ts, price=excluded.price, bid=excluded.bid, ask=excluded.ask,
                spread_bps=excluded.spread_bps, source=excluded.source
            """, (market, symbol, ts, price, bid, ask, spread, source))
            if price is not None:
                c.execute("INSERT INTO ticks(market,symbol,ts,price,bid,ask,source) VALUES(?,?,?,?,?,?,?)",
                          (market, symbol, ts, price, bid, ask, source))
        return {"market": market, "symbol": symbol, "ts": ts, "price": price, "bid": bid, "ask": ask,
                "spread_bps": spread, "source": source}

    def quotes(self):
        with self._c() as c:
            return [dict(x) for x in c.execute("SELECT * FROM quotes ORDER BY market,symbol")]

    def set_signal(self, market, symbol, signal, priority=0, detail="", confidence=None, ts=None):
        with self._c() as c:
            c.execute("""
              INSERT INTO signals(market,symbol,ts,signal,priority,detail,confidence)
              VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(market,symbol) DO UPDATE SET
                ts=excluded.ts, signal=excluded.signal, priority=excluded.priority,
                detail=excluded.detail, confidence=excluded.confidence
            """, (market, str(symbol).upper(), ts or now_iso(), signal, int(priority), detail,
                  float(confidence) if confidence is not None else None))

    def signals(self):
        with self._c() as c:
            return [dict(x) for x in c.execute("SELECT * FROM signals ORDER BY priority DESC,ts DESC")]

    def prune_ticks(self, keep_hours=6):
        with self._c() as c:
            c.execute("DELETE FROM ticks WHERE julianday('now') - julianday(ts) > ?", (float(keep_hours) / 24.0,))


def _market_from_account_id(account_id: str) -> str:
    aid = str(account_id or "")
    for market in ("crypto", "stock", "twstock"):
        if aid.startswith(market + "_"):
            return market
    return aid.rsplit("_", 1)[0] if "_" in aid else ""


def build_realtime_watchlist(sim_db, realtime_db: RealtimeDB):
    """Positions are always watched; remaining slots use latest confidence with ACTIVE-asset fallback."""
    positions = sim_db.positions()
    position_keys = set()
    rows = []
    for p in positions:
        market = _market_from_account_id(p.get("account_id"))
        symbol = str(p.get("symbol") or "").upper()
        if market and symbol:
            position_keys.add((market, symbol))
            rows.append({"market": market, "symbol": symbol, "score": 1000.0, "reason": "POSITION"})

    latest = {}
    for d in sim_db.recent_decisions(5000):
        market = str(d.get("market") or "")
        symbol = str(d.get("symbol") or "").upper()
        horizon = str(d.get("horizon") or "")
        key = (market, symbol, horizon)
        if not market or not symbol or key in latest:
            continue
        latest[key] = d

    score_by_symbol = {}
    action_by_symbol = {}
    for (market, symbol, _), d in latest.items():
        conf = float(d.get("confidence") or 0)
        key = (market, symbol)
        if conf > score_by_symbol.get(key, -1):
            score_by_symbol[key] = conf
            action_by_symbol[key] = str(d.get("action") or "")

    # Fallback: if a newly deployed realtime worker starts before decisions are available,
    # seed it from ACTIVE simulation assets instead of leaving the entire stream idle at 0.
    active_assets = sim_db.assets()
    active_by_market = {"crypto": [], "stock": [], "twstock": []}
    for a in active_assets:
        market = str(a.get("market") or "")
        symbol = str(a.get("symbol") or "").upper()
        if market in active_by_market and symbol:
            active_by_market[market].append(symbol)

    for market in ("crypto", "stock", "twstock"):
        cap = _cap(market)
        already = sum(1 for m, _ in position_keys if m == market)
        slots = max(0, cap - already)
        candidates = [
            (score, symbol, action_by_symbol.get((market, symbol), ""), "DECISION")
            for (m, symbol), score in score_by_symbol.items()
            if m == market and (market, symbol) not in position_keys
        ]
        selected_symbols = {symbol for _, symbol, _, _ in candidates}
        # Add ACTIVE assets not represented in latest decisions as low-priority fallback.
        for symbol in active_by_market.get(market, []):
            if (market, symbol) in position_keys or symbol in selected_symbols:
                continue
            candidates.append((1.0, symbol, "WATCH", "ACTIVE_FALLBACK"))
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for score, symbol, action, source in candidates[:slots]:
            reason = f"TOP_CONFIDENCE:{action or 'WATCH'}" if source == "DECISION" else "ACTIVE_FALLBACK"
            rows.append({"market": market, "symbol": symbol, "score": score, "reason": reason})

    realtime_db.set_watchlist(rows)
    return rows


def evaluate_realtime_signal(sim_db, realtime_db: RealtimeDB, market: str, symbol: str, quote: dict):
    price = quote.get("price")
    if price is None or price <= 0:
        return
    positions = []
    for p in sim_db.positions():
        pm = _market_from_account_id(p.get("account_id"))
        if pm == market and str(p.get("symbol") or "").upper() == symbol.upper():
            positions.append(p)

    if positions:
        best_signal = ("HOLD", 10, "持倉秒級監控")
        for p in positions:
            stop = float(p.get("stop_price") or 0)
            target = float(p.get("target_price") or 0)
            if stop > 0 and price <= stop:
                cand = ("STOP_TOUCH", 100, f"價格 {price:.8g} 已碰停損 {stop:.8g}")
            elif target > 0 and price >= target:
                cand = ("TARGET_TOUCH", 95, f"價格 {price:.8g} 已碰目標 {target:.8g}")
            elif stop > 0 and price / stop - 1 <= 0.01:
                cand = ("NEAR_STOP", 80, f"距停損 {(price/stop-1)*100:.2f}%")
            elif target > 0 and target / price - 1 <= 0.01:
                cand = ("NEAR_TARGET", 70, f"距目標 {(target/price-1)*100:.2f}%")
            else:
                cand = ("HOLD", 10, "持倉秒級監控")
            if cand[1] > best_signal[1]:
                best_signal = cand
        realtime_db.set_signal(market, symbol, *best_signal, ts=quote.get("ts"))
        return

    confidence = None
    action = ""
    for d in sim_db.recent_decisions(1000):
        if str(d.get("market") or "") == market and str(d.get("symbol") or "").upper() == symbol.upper():
            c = float(d.get("confidence") or 0)
            if confidence is None or c > confidence:
                confidence = c
                action = str(d.get("action") or "")
    spread = quote.get("spread_bps")
    if action == "ENTER" and (spread is None or spread <= 30):
        realtime_db.set_signal(market, symbol, "ENTRY_CONFIRM", 60,
                               f"模型 ENTER；即時 spread {spread:.1f} bps" if spread is not None else "模型 ENTER；等待下一根執行",
                               confidence, quote.get("ts"))
    else:
        realtime_db.set_signal(market, symbol, "WATCH", 5,
                               f"高信心候選；模型 {action or 'WATCH'}", confidence, quote.get("ts"))
