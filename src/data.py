from __future__ import annotations
import os
import time
import requests
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional

OHLCV = ["open", "high", "low", "close", "volume"]

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    missing = [c for c in OHLCV if c not in out.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")
    out = out[OHLCV].astype(float)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna()

@dataclass
class AlpacaData:
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    base_url: str = "https://data.alpaca.markets"

    def __post_init__(self):
        self.api_key = self.api_key or os.getenv("ALPACA_API_KEY")
        self.api_secret = self.api_secret or os.getenv("ALPACA_API_SECRET")
        self.base_url = os.getenv("ALPACA_DATA_BASE_URL", self.base_url)

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def bars(self, symbol: str, start: str, end: str, timeframe: str = "1Day",
             adjustment: str = "all", feed: str = "iex") -> pd.DataFrame:
        if not self.ready:
            raise RuntimeError("Alpaca API keys are not configured.")
        url = f"{self.base_url}/v2/stocks/{symbol}/bars"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }
        params = {
            "start": start, "end": end, "timeframe": timeframe,
            "adjustment": adjustment, "feed": feed, "limit": 10000,
        }
        rows, page_token = [], None
        while True:
            if page_token:
                params["page_token"] = page_token
            r = requests.get(url, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            payload = r.json()
            rows.extend(payload.get("bars", []))
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            time.sleep(0.05)
        if not rows:
            return pd.DataFrame(columns=OHLCV)
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["t"], utc=True)
        df = df.set_index("timestamp").rename(columns={
            "o":"open","h":"high","l":"low","c":"close","v":"volume"
        })
        return _normalize(df)

    def assets(self, status: str = "active", asset_class: str = "us_equity") -> pd.DataFrame:
        """Current Alpaca asset master list. Requires Alpaca credentials."""
        if not self.ready:
            raise RuntimeError("Alpaca API keys are not configured.")
        trading_base = os.getenv("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets")
        url = f"{trading_base}/v2/assets"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }
        params = {"status": status, "asset_class": asset_class}
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return pd.DataFrame(r.json())

    def snapshots(self, symbols: list[str], feed: str = "iex", batch_size: int = 100) -> pd.DataFrame:
        """
        Latest snapshot metadata for many US symbols.
        Liquidity fields are feed-dependent; with feed=iex they are IEX-only proxies.
        """
        if not self.ready:
            raise RuntimeError("Alpaca API keys are not configured.")
        url = f"{self.base_url}/v2/stocks/snapshots"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }
        rows = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            if not batch:
                continue
            r = requests.get(
                url, headers=headers,
                params={"symbols": ",".join(batch), "feed": feed},
                timeout=30
            )
            r.raise_for_status()
            payload = r.json()
            for sym, snap in payload.items():
                daily = snap.get("dailyBar") or {}
                prev = snap.get("prevDailyBar") or {}
                trade = snap.get("latestTrade") or {}
                quote = snap.get("latestQuote") or {}
                price = trade.get("p") or daily.get("c") or prev.get("c")
                volume = daily.get("v") or 0
                rows.append({
                    "symbol": sym,
                    "price": float(price) if price is not None else np.nan,
                    "daily_volume": float(volume) if volume is not None else np.nan,
                    "dollar_volume_proxy": (float(price) * float(volume)) if price is not None and volume is not None else np.nan,
                    "bid": float(quote.get("bp")) if quote.get("bp") is not None else np.nan,
                    "ask": float(quote.get("ap")) if quote.get("ap") is not None else np.nan,
                    "prev_close": float(prev.get("c")) if prev.get("c") is not None else np.nan,
                })
            time.sleep(0.03)
        return pd.DataFrame(rows)

    def most_active(self, top: int = 100, by: str = "volume") -> pd.DataFrame:
        """Alpaca stock screener. Official endpoint supports top <= 100."""
        if not self.ready:
            raise RuntimeError("Alpaca API keys are not configured.")
        url = f"{self.base_url}/v1beta1/screener/stocks/most-actives"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }
        r = requests.get(
            url, headers=headers,
            params={"top": min(max(int(top), 1), 100), "by": by},
            timeout=30
        )
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("most_actives", payload if isinstance(payload, list) else [])
        return pd.DataFrame(rows)

    def bars_many(self, symbols: list[str], start: str, end: str,
                  timeframe: str = "1Day", adjustment: str = "all",
                  feed: str = "iex", batch_size: int = 50) -> dict[str, pd.DataFrame]:
        """
        Batch historical bars via Alpaca's multi-symbol endpoint.
        Returned data is split into normalized per-symbol DataFrames.
        """
        if not self.ready:
            raise RuntimeError("Alpaca API keys are not configured.")
        url = f"{self.base_url}/v2/stocks/bars"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        for bi in range(0, len(symbols), batch_size):
            batch = symbols[bi:bi+batch_size]
            params = {
                "symbols": ",".join(batch),
                "start": start, "end": end, "timeframe": timeframe,
                "adjustment": adjustment, "feed": feed, "limit": 10000,
                "sort": "asc",
            }
            page_token = None
            while True:
                if page_token:
                    params["page_token"] = page_token
                elif "page_token" in params:
                    params.pop("page_token")
                r = requests.get(url, headers=headers, params=params, timeout=45)
                r.raise_for_status()
                payload = r.json()
                bars = payload.get("bars", {})
                if isinstance(bars, dict):
                    for sym, vals in bars.items():
                        out.setdefault(sym, []).extend(vals or [])
                page_token = payload.get("next_page_token")
                if not page_token:
                    break
                time.sleep(0.04)
            time.sleep(0.04)

        normalized = {}
        for sym, rows in out.items():
            if not rows:
                normalized[sym] = pd.DataFrame(columns=OHLCV)
                continue
            df = pd.DataFrame(rows)
            df["timestamp"] = pd.to_datetime(df["t"], utc=True)
            df = df.set_index("timestamp").rename(columns={
                "o":"open","h":"high","l":"low","c":"close","v":"volume"
            })
            normalized[sym] = _normalize(df)
        return normalized
@dataclass
class BinanceData:
    base_url: str = "https://api.binance.com"

    def bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        url = f"{self.base_url}/api/v3/klines"
        start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000) if pd.Timestamp(start).tzinfo is None else int(pd.Timestamp(start).timestamp() * 1000)
        end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if pd.Timestamp(end).tzinfo is None else int(pd.Timestamp(end).timestamp() * 1000)
        rows = []
        cursor = start_ms
        while cursor < end_ms:
            params = {"symbol": symbol.upper(), "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000}
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            last_open = int(batch[-1][0])
            if last_open <= cursor:
                break
            cursor = last_open + 1
            time.sleep(0.05)
        if not rows:
            return pd.DataFrame(columns=OHLCV)
        data = pd.DataFrame(rows, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "quote_volume","trades","taker_base","taker_quote","ignore"
        ])
        data["timestamp"] = pd.to_datetime(data["open_time"], unit="ms", utc=True)
        data = data.set_index("timestamp")
        return _normalize(data)


    def exchange_info(self) -> pd.DataFrame:
        r = requests.get(
            f"{self.base_url}/api/v3/exchangeInfo",
            params={"showPermissionSets": "false"},
            timeout=30
        )
        r.raise_for_status()
        return pd.DataFrame(r.json().get("symbols", []))

    def ticker_24h(self) -> pd.DataFrame:
        r = requests.get(f"{self.base_url}/api/v3/ticker/24hr", timeout=30)
        r.raise_for_status()
        return pd.DataFrame(r.json())

    def discover_spot_universe(
        self, quote_asset: str = "USDT", min_quote_volume: float = 1_000_000,
        max_symbols: int = 300
    ) -> pd.DataFrame:
        info = self.exchange_info()
        tick = self.ticker_24h()
        if info.empty or tick.empty:
            return pd.DataFrame()
        keep = info[
            (info["status"] == "TRADING")
            & (info["quoteAsset"] == quote_asset)
        ].copy()
        if "isSpotTradingAllowed" in keep.columns:
            keep = keep[keep["isSpotTradingAllowed"] == True]
        merged = keep.merge(
            tick[["symbol","lastPrice","quoteVolume","count"]],
            on="symbol", how="left"
        )
        for c in ["lastPrice","quoteVolume","count"]:
            merged[c] = pd.to_numeric(merged[c], errors="coerce")
        merged = merged[
            (merged["lastPrice"] > 0)
            & (merged["quoteVolume"] >= float(min_quote_volume))
        ]
        merged = merged.sort_values(
            ["quoteVolume","count"], ascending=False
        ).head(int(max_symbols))
        return merged.reset_index(drop=True)
def validate_ohlcv(df: pd.DataFrame) -> dict:
    """Strict structural checks before research. Returns counts; non-zero critical counts should be investigated."""
    if df is None or len(df) == 0:
        return {"rows": 0, "duplicates": 0, "missing": 0, "bad_high": 0, "bad_low": 0,
                "nonpositive_price": 0, "negative_volume": 0, "non_monotonic_time": 0}
    x = df.copy()
    return {
        "rows": int(len(x)),
        "duplicates": int(x.index.duplicated().sum()),
        "missing": int(x[OHLCV].isna().sum().sum()),
        "bad_high": int((x["high"] < x[["open","close","low"]].max(axis=1)).sum()),
        "bad_low": int((x["low"] > x[["open","close","high"]].min(axis=1)).sum()),
        "nonpositive_price": int((x[["open","high","low","close"]] <= 0).any(axis=1).sum()),
        "negative_volume": int((x["volume"] < 0).sum()),
        "non_monotonic_time": int(not x.index.is_monotonic_increasing),
    }
