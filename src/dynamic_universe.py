from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from .data import AlpacaData, BinanceData
from .paths import data_dir
from .twstock_support import TW_MARKET, normalize_tw_symbol

DEFAULT_LIMITS = {"crypto": 30, "stock": 50, TW_MARKET: 30}
DEFAULT_REFRESH_HOURS = {"crypto": 6, "stock": 6, TW_MARKET: 24}
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDS", "BUSD"}
STATE_PATH = Path(data_dir()) / "dynamic_universe.json"
TWSE_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"


def _utcnow():
    return datetime.now(timezone.utc)


def _read_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(payload):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return 0.0


def _limit(market):
    key = {"crypto": "V6_UNIVERSE_CRYPTO", "stock": "V6_UNIVERSE_STOCK", TW_MARKET: "V6_UNIVERSE_TWSTOCK"}[market]
    try:
        return max(1, int(os.getenv(key, DEFAULT_LIMITS[market])))
    except Exception:
        return DEFAULT_LIMITS[market]


def _refresh_hours(market):
    key = {"crypto": "V6_UNIVERSE_CRYPTO_HOURS", "stock": "V6_UNIVERSE_STOCK_HOURS", TW_MARKET: "V6_UNIVERSE_TWSTOCK_HOURS"}[market]
    try:
        return max(1, int(os.getenv(key, DEFAULT_REFRESH_HOURS[market])))
    except Exception:
        return DEFAULT_REFRESH_HOURS[market]


def _due(state, market, now):
    raw = ((state.get("markets") or {}).get(market) or {}).get("updated_at")
    if not raw:
        return True
    try:
        t = pd.Timestamp(raw)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return (pd.Timestamp(now) - t).total_seconds() >= _refresh_hours(market) * 3600
    except Exception:
        return True


def discover_crypto(limit):
    df = BinanceData().discover_spot_universe(max_symbols=max(limit * 3, 100))
    if df.empty:
        return []
    if "baseAsset" in df.columns:
        df = df[~df["baseAsset"].astype(str).str.upper().isin(STABLE_BASES)]
    return df["symbol"].astype(str).str.upper().drop_duplicates().head(limit).tolist()


def discover_stocks(limit):
    api = AlpacaData()
    act = api.most_active(top=100, by="volume")
    if act.empty or "symbol" not in act.columns:
        return []
    syms = act["symbol"].astype(str).str.upper().drop_duplicates().tolist()
    snap = api.snapshots(syms)
    if snap.empty:
        return syms[:limit]
    snap["dollar_volume_proxy"] = pd.to_numeric(snap.get("dollar_volume_proxy"), errors="coerce").fillna(0.0)
    snap["price"] = pd.to_numeric(snap.get("price"), errors="coerce").fillna(0.0)
    snap = snap[(snap["price"] >= 2.0) & (snap["dollar_volume_proxy"] > 0)]
    return snap.sort_values("dollar_volume_proxy", ascending=False)["symbol"].astype(str).str.upper().drop_duplicates().head(limit).tolist()


def discover_twstocks(limit):
    r = requests.get(TWSE_ALL_URL, timeout=30, headers={"User-Agent": "V6-Quant-Lab/1.0"})
    r.raise_for_status()
    rows = r.json()
    scored = []
    for row in rows if isinstance(rows, list) else []:
        code = str(row.get("Code") or row.get("證券代號") or "").strip()
        if not code or not code.isdigit():
            continue
        value = _num(row.get("TradeValue") or row.get("成交金額") or 0)
        volume = _num(row.get("TradeVolume") or row.get("成交股數") or 0)
        close = _num(row.get("ClosingPrice") or row.get("收盤價") or 0)
        if close <= 0 or value <= 0 or volume <= 0:
            continue
        scored.append((value, normalize_tw_symbol(code)))
    scored.sort(reverse=True)
    out = []
    seen = set()
    for _, symbol in scored:
        if symbol not in seen:
            out.append(symbol)
            seen.add(symbol)
        if len(out) >= limit:
            break
    return out


def _position_symbols(db, market):
    out = set()
    for p in db.positions():
        aid = str(p.get("account_id") or "")
        if aid == market or aid.startswith(f"{market}_"):
            out.add(str(p.get("symbol") or "").upper())
    return out


def _sync_market(db, market, selected, pinned=None):
    selected = {str(s).upper() for s in (selected or []) if str(s).strip()}
    pinned = {str(s).upper() for s in (pinned or []) if str(s).strip()}
    keep = selected | pinned | _position_symbols(db, market)
    if not keep:
        return {"active": 0, "added": 0, "deactivated": 0}

    before = {r["symbol"] for r in db.assets() if r.get("market") == market}
    with db._c() as c:
        c.execute("UPDATE assets SET status='INACTIVE' WHERE market=?", (market,))
        for sym in sorted(keep):
            c.execute(
                "INSERT INTO assets(market,symbol,status,added_at) VALUES(?,?, 'ACTIVE', ?) "
                "ON CONFLICT(market,symbol) DO UPDATE SET status='ACTIVE'",
                (market, sym, _utcnow().isoformat()),
            )
    after = {r["symbol"] for r in db.assets() if r.get("market") == market}
    return {
        "active": len(after),
        "added": len(after - before),
        "deactivated": len(before - after),
    }


class DynamicUniverse:
    def __init__(self, db):
        self.db = db

    def refresh_due(self, pinned=None, force=False):
        now = pd.Timestamp.now(tz="UTC")
        state = _read_state()
        state.setdefault("markets", {})
        pinned = pinned or {}
        results = {}
        single_crypto = os.getenv("V6_SINGLE_CRYPTO_ACCOUNT","0").strip().lower() in ("1","true","yes","on")
        discoverers = (("crypto", discover_crypto),) if single_crypto else (
            ("crypto", discover_crypto), ("stock", discover_stocks), (TW_MARKET, discover_twstocks)
        )
        for market, discover in discoverers:
            if not force and not _due(state, market, now):
                current = [a["symbol"] for a in self.db.assets() if a.get("market") == market]
                results[market] = {"status": "SKIPPED", "active": len(current), "limit": _limit(market)}
                continue
            try:
                limit = _limit(market)
                selected = discover(limit)
                if not selected:
                    raise RuntimeError("universe discovery returned no symbols")
                sync = _sync_market(self.db, market, selected, pinned.get(market, []))
                state["markets"][market] = {
                    "updated_at": now.isoformat(),
                    "limit": limit,
                    "selected": selected,
                    "status": "OK",
                }
                results[market] = {"status": "OK", "limit": limit, "selected": len(selected), **sync}
            except Exception as exc:
                current = [a["symbol"] for a in self.db.assets() if a.get("market") == market]
                results[market] = {
                    "status": "ERROR",
                    "active": len(current),
                    "limit": _limit(market),
                    "error": f"{type(exc).__name__}: {exc}",
                }
        state["last_refresh_attempt"] = now.isoformat()
        state["last_result"] = results
        _write_state(state)
        return results
