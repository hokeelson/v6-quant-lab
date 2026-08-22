from __future__ import annotations
import sqlite3
from pathlib import Path
import pandas as pd

from .data import AlpacaData, BinanceData

TIMEFRAME_MAP = {
    ("stock", "short"): ("1Hour", "1h"),
    ("stock", "medium"): ("4Hour", "4h"),
    ("stock", "long"): ("1Day", "1d"),
    ("crypto", "short"): ("1Hour", "1h"),
    ("crypto", "medium"): ("4Hour", "4h"),
    ("crypto", "long"): ("1Day", "1d"),
}

HISTORY_DAYS = {"short": 180, "medium": 900, "long": 3000}


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class MarketCache:
    """SQLite OHLCV cache. API calls only request the missing tail after cached data."""
    def __init__(self, path: str = "market_cache.sqlite3"):
        self.path = str(path)
        self._init()

    def _connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init(self):
        with self._connect() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS bars(
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                ts TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY(market,symbol,timeframe,ts)
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars(market,symbol,timeframe,ts)")
            c.execute("""CREATE TABLE IF NOT EXISTS fetch_state(
                market TEXT NOT NULL, symbol TEXT NOT NULL, horizon TEXT NOT NULL,
                last_attempt TEXT, PRIMARY KEY(market,symbol,horizon))""")

    def get(self, market: str, symbol: str, timeframe: str, start=None, end=None) -> pd.DataFrame:
        q = "SELECT ts,open,high,low,close,volume FROM bars WHERE market=? AND symbol=? AND timeframe=?"
        args = [market, symbol.upper(), timeframe]
        if start is not None:
            q += " AND ts>=?"; args.append(_utc(start).isoformat())
        if end is not None:
            q += " AND ts<=?"; args.append(_utc(end).isoformat())
        q += " ORDER BY ts"
        with self._connect() as c:
            rows = c.execute(q, args).fetchall()
        if not rows:
            return pd.DataFrame(columns=["open","high","low","close","volume"])
        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df.pop("ts"), utc=True)
        return df.set_index("timestamp")[["open","high","low","close","volume"]].astype(float)

    def last_timestamp(self, market: str, symbol: str, timeframe: str):
        with self._connect() as c:
            row = c.execute(
                "SELECT MAX(ts) AS ts FROM bars WHERE market=? AND symbol=? AND timeframe=?",
                (market, symbol.upper(), timeframe),
            ).fetchone()
        return pd.Timestamp(row["ts"]) if row and row["ts"] else None

    def upsert(self, market: str, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = []
        for ts, r in df.iterrows():
            rows.append((market, symbol.upper(), timeframe, _utc(ts).isoformat(),
                         float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)))
        with self._connect() as c:
            c.executemany("""
                INSERT INTO bars(market,symbol,timeframe,ts,open,high,low,close,volume)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(market,symbol,timeframe,ts) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume
            """, rows)
        return len(rows)

    def _last_attempt(self, market, symbol, horizon):
        with self._connect() as c:
            r=c.execute("SELECT last_attempt FROM fetch_state WHERE market=? AND symbol=? AND horizon=?",(market,symbol.upper(),horizon)).fetchone()
        return _utc(r["last_attempt"]) if r and r["last_attempt"] else None

    def _set_attempt(self, market, symbol, horizon, ts):
        with self._connect() as c:
            c.execute("INSERT INTO fetch_state(market,symbol,horizon,last_attempt) VALUES(?,?,?,?) ON CONFLICT(market,symbol,horizon) DO UPDATE SET last_attempt=excluded.last_attempt",(market,symbol.upper(),horizon,_utc(ts).isoformat()))

    def ensure(self, market: str, symbol: str, horizon: str, now=None, force_history: bool = False, min_refresh_seconds: int | None = None) -> dict:
        now = _utc(now or pd.Timestamp.now(tz="UTC"))
        alpaca_tf, binance_tf = TIMEFRAME_MAP[(market, horizon)]
        tf_key = alpaca_tf if market == "stock" else binance_tf
        last = self.last_timestamp(market, symbol, tf_key)
        history_start = now - pd.Timedelta(days=HISTORY_DAYS[horizon])
        if min_refresh_seconds is None:
            min_refresh_seconds = {
                ("crypto","short"):60,("crypto","medium"):300,("crypto","long"):1800,
                ("stock","short"):300,("stock","medium"):900,("stock","long"):3600,
            }[(market,horizon)]
        last_attempt=self._last_attempt(market,symbol,horizon)
        if (not force_history) and last_attempt is not None and (now-last_attempt).total_seconds() < min_refresh_seconds:
            return {"fetched":0,"api_called":False,"timeframe":tf_key,"data":self.get(market,symbol,tf_key,history_start,now)}
        if force_history or last is None:
            start = history_start
        else:
            # overlap two bars so revised/partial bars are safely replaced
            overlap = {"short": pd.Timedelta(hours=3), "medium": pd.Timedelta(hours=12), "long": pd.Timedelta(days=3)}[horizon]
            start = max(history_start, _utc(last) - overlap)
        if start >= now:
            return {"fetched":0,"api_called":False,"timeframe":tf_key,"data":self.get(market,symbol,tf_key,history_start,now)}
        self._set_attempt(market,symbol,horizon,now)
        if market == "stock":
            new = AlpacaData().bars(symbol, start.isoformat(), now.isoformat(), timeframe=alpaca_tf, adjustment="all", feed="iex")
        else:
            new = BinanceData().bars(symbol, start.isoformat(), now.isoformat(), interval=binance_tf)
        fetched = self.upsert(market, symbol, tf_key, new)
        return {"fetched":fetched, "timeframe":tf_key, "data":self.get(market,symbol,tf_key,history_start,now)}

    @staticmethod
    def closed_only(df: pd.DataFrame, market: str, horizon: str, now=None) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        now = _utc(now or pd.Timestamp.now(tz="UTC"))
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        if horizon == "long":
            if market == "crypto":
                mask = idx.date < now.floor("D").date()
            else:
                ny_today = now.tz_convert("America/New_York").date()
                mask = [x.tz_convert("America/New_York").date() < ny_today for x in idx]
        else:
            delta = pd.Timedelta(hours=1 if horizon == "short" else 4)
            mask = idx + delta <= now
        return df.loc[mask].copy()
