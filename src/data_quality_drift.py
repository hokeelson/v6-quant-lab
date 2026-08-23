from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .data import validate_ohlcv
from .market_cache import HISTORY_DAYS, TIMEFRAME_MAP

TW_MARKET = "twstock"
_HORIZONS = ("short", "medium", "long")


def _utc(value=None) -> pd.Timestamp:
    t = pd.Timestamp(value or datetime.now(timezone.utc))
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _finite(value, default=None):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _timeframe_key(market: str, horizon: str) -> str:
    pair = TIMEFRAME_MAP.get((market, horizon))
    if pair:
        if market == "stock":
            return str(pair[0])
        return str(pair[1])
    # Taiwan mapping is normally injected by twstock_support during startup.
    if market == TW_MARKET:
        return {"short": "1h", "medium": "1d", "long": "1wk"}[horizon]
    raise KeyError((market, horizon))


def _stale_limit_hours(market: str, horizon: str) -> float:
    # Intentionally lenient for exchange closures/holidays so historical quality
    # monitoring does not mistake a weekend for a broken feed.
    table = {
        "crypto": {"short": 4.0, "medium": 16.0, "long": 60.0},
        "stock": {"short": 120.0, "medium": 120.0, "long": 240.0},
        TW_MARKET: {"short": 120.0, "medium": 240.0, "long": 504.0},
    }
    return float(table.get(market, table["stock"])[horizon])


def _atr_pct(df: pd.DataFrame, n: int = 14) -> float | None:
    if df is None or len(df) < n + 2:
        return None
    pc = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    value = (tr / df["close"].replace(0, np.nan)).rolling(n, min_periods=n).mean().iloc[-1]
    return _finite(value)


def _trend_state(df: pd.DataFrame) -> str:
    if df is None or len(df) < 30:
        return "UNKNOWN"
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(c) < 30:
        return "UNKNOWN"
    fast = c.ewm(span=12, adjust=False).mean()
    slow = c.ewm(span=30, adjust=False).mean()
    ret = c.pct_change().dropna()
    vol = float(ret.tail(30).std(ddof=1) or 0.0)
    edge = float(fast.iloc[-1] / slow.iloc[-1] - 1.0) if slow.iloc[-1] else 0.0
    band = max(0.002, 0.35 * vol)
    if edge > band:
        return "UP"
    if edge < -band:
        return "DOWN"
    return "SIDEWAYS"


def _ks_stat(a: np.ndarray, b: np.ndarray) -> float | None:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 20 or len(b) < 20:
        return None
    try:
        from scipy.stats import ks_2samp
        return float(ks_2samp(a, b, alternative="two-sided", method="auto").statistic)
    except Exception:
        # Dependency-safe empirical KS fallback.
        values = np.sort(np.unique(np.concatenate([a, b])))
        if len(values) == 0:
            return None
        ca = np.searchsorted(np.sort(a), values, side="right") / len(a)
        cb = np.searchsorted(np.sort(b), values, side="right") / len(b)
        return float(np.max(np.abs(ca - cb)))


def _quality_assessment(df: pd.DataFrame, market: str, horizon: str, now: pd.Timestamp) -> dict:
    if df is None or df.empty:
        return {
            "data_status": "CRITICAL", "quality_score": 0.0, "data_multiplier": 0.40,
            "last_bar": None, "stale_hours": None, "stale_limit_hours": _stale_limit_hours(market, horizon),
            "structural": {"rows": 0}, "extreme_return_bars": 0, "zero_volume_ratio": None,
            "reasons": ["no_closed_bars"],
        }

    structural = validate_ohlcv(df)
    critical = int(sum(structural.get(k, 0) for k in [
        "duplicates", "missing", "bad_high", "bad_low", "nonpositive_price",
        "negative_volume", "non_monotonic_time",
    ]))
    last = _utc(df.index[-1])
    stale_hours = max(0.0, (now - last).total_seconds() / 3600.0)
    stale_limit = _stale_limit_hours(market, horizon)

    recent = df.tail(160).copy()
    rets = pd.to_numeric(recent["close"], errors="coerce").pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    extreme = 0
    if len(rets) >= 20:
        med = float(rets.median())
        mad = float((rets - med).abs().median())
        robust_scale = max(1e-6, 1.4826 * mad)
        threshold = np.maximum(0.15, 12.0 * robust_scale)
        extreme = int((np.abs(rets - med) > threshold).sum())

    volume = pd.to_numeric(recent["volume"], errors="coerce")
    zero_volume_ratio = float((volume <= 0).mean()) if len(volume) else None
    rows = int(structural.get("rows", len(df)))

    score = 100.0
    reasons = []
    if critical:
        score -= min(70.0, critical * 20.0)
        reasons.append(f"structural_errors:{critical}")
    if stale_hours > stale_limit:
        score -= 35.0
        reasons.append("stale_history")
    if stale_hours > stale_limit * 2.0:
        score -= 20.0
        reasons.append("severely_stale_history")
    if extreme >= 4:
        score -= min(25.0, 5.0 * extreme)
        reasons.append(f"extreme_return_bars:{extreme}")
    if zero_volume_ratio is not None and zero_volume_ratio > 0.25:
        score -= 15.0
        reasons.append("high_zero_volume_ratio")
    if rows < 80:
        score -= 20.0
        reasons.append("thin_history")
    score = float(np.clip(score, 0.0, 100.0))

    if critical > 0 or stale_hours > stale_limit * 2.0:
        status, multiplier = "CRITICAL", 0.40
    elif stale_hours > stale_limit or extreme >= 4 or (zero_volume_ratio or 0.0) > 0.25 or rows < 80:
        status, multiplier = "WARNING", 0.85
    else:
        status, multiplier = "OK", 1.00

    return {
        "data_status": status,
        "quality_score": score,
        "data_multiplier": multiplier,
        "last_bar": last.isoformat(),
        "stale_hours": stale_hours,
        "stale_limit_hours": stale_limit,
        "structural": structural,
        "extreme_return_bars": extreme,
        "zero_volume_ratio": zero_volume_ratio,
        "reasons": reasons,
    }


def _drift_assessment(df: pd.DataFrame, model: dict | None, horizon: str) -> dict:
    if not model:
        return {
            "drift_status": "NO_MODEL", "drift_score": 0.0, "drift_multiplier": 1.0,
            "post_calibration_bars": 0, "baseline_bars": 0, "recent_bars": 0,
            "metrics": {}, "reasons": ["no_model"],
        }
    if df is None or len(df) < 50:
        return {
            "drift_status": "LEARNING", "drift_score": 0.0, "drift_multiplier": 1.0,
            "post_calibration_bars": 0, "baseline_bars": 0, "recent_bars": int(len(df) if df is not None else 0),
            "metrics": {}, "reasons": ["insufficient_history"],
        }

    try:
        calibrated = _utc(model.get("calibrated_through") or model.get("updated_at"))
    except Exception:
        calibrated = _utc(df.index[-1])

    x = df.copy()
    idx = pd.DatetimeIndex(x.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    x.index = idx

    baseline_n = {"short": 180, "medium": 140, "long": 120}[horizon]
    recent_n = {"short": 80, "medium": 60, "long": 40}[horizon]
    min_post = {"short": 12, "medium": 8, "long": 4}[horizon]
    baseline = x[x.index <= calibrated].tail(baseline_n)
    recent = x.tail(recent_n)
    post_count = int((x.index > calibrated).sum())

    if len(baseline) < min(80, baseline_n) or len(recent) < min(30, recent_n) or post_count < min_post:
        return {
            "drift_status": "LEARNING", "drift_score": 0.0, "drift_multiplier": 1.0,
            "post_calibration_bars": post_count, "baseline_bars": int(len(baseline)), "recent_bars": int(len(recent)),
            "metrics": {}, "reasons": ["not_enough_post_calibration_evidence"],
        }

    br = baseline["close"].pct_change().replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)
    rr = recent["close"].pct_change().replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)
    bvol = float(np.std(br, ddof=1)) if len(br) > 1 else 0.0
    rvol = float(np.std(rr, ddof=1)) if len(rr) > 1 else 0.0
    vol_ratio = rvol / max(bvol, 1e-8)
    mean_shift = abs(float(np.mean(rr)) - float(np.mean(br))) / max(bvol, 1e-8)
    ks = _ks_stat(br, rr)
    batr = _atr_pct(baseline)
    ratr = _atr_pct(recent)
    atr_ratio = (ratr / max(batr, 1e-8)) if batr is not None and ratr is not None else 1.0
    baseline_trend = _trend_state(baseline)
    recent_trend = _trend_state(recent)
    trend_changed = baseline_trend != "UNKNOWN" and recent_trend != "UNKNOWN" and baseline_trend != recent_trend

    def ratio_severity(ratio: float) -> float:
        ratio = max(float(ratio), 1e-8)
        return float(np.clip(abs(math.log(ratio)) / math.log(2.5) * 100.0, 0.0, 100.0))

    vol_sev = ratio_severity(vol_ratio)
    atr_sev = ratio_severity(atr_ratio)
    ks_sev = float(np.clip(((ks or 0.0) - 0.10) / 0.30 * 100.0, 0.0, 100.0))
    mean_sev = float(np.clip(mean_shift / 0.40 * 100.0, 0.0, 100.0))
    trend_sev = 100.0 if trend_changed else 0.0
    score = float(np.clip(
        0.35 * vol_sev + 0.25 * ks_sev + 0.20 * atr_sev + 0.10 * mean_sev + 0.10 * trend_sev,
        0.0, 100.0,
    ))

    reasons = []
    if vol_sev >= 55:
        reasons.append("volatility_shift")
    if ks_sev >= 55:
        reasons.append("return_distribution_shift")
    if atr_sev >= 55:
        reasons.append("atr_shift")
    if mean_sev >= 55:
        reasons.append("return_mean_shift")
    if trend_changed:
        reasons.append("trend_state_changed")

    if score >= 80:
        status, multiplier = "SEVERE", 0.40
    elif score >= 60:
        status, multiplier = "DRIFT", 0.60
    elif score >= 40:
        status, multiplier = "WATCH", 0.85
    else:
        status, multiplier = "NORMAL", 1.00

    return {
        "drift_status": status,
        "drift_score": score,
        "drift_multiplier": multiplier,
        "post_calibration_bars": post_count,
        "baseline_bars": int(len(baseline)),
        "recent_bars": int(len(recent)),
        "metrics": {
            "baseline_volatility": bvol,
            "recent_volatility": rvol,
            "volatility_ratio": vol_ratio,
            "ks_statistic": ks,
            "mean_shift_normalized": mean_shift,
            "baseline_atr_pct": batr,
            "recent_atr_pct": ratr,
            "atr_ratio": atr_ratio,
            "baseline_trend": baseline_trend,
            "recent_trend": recent_trend,
            "trend_changed": trend_changed,
            "calibrated_through": calibrated.isoformat(),
        },
        "reasons": reasons,
    }


def assess_pair(db, cache, market: str, symbol: str, horizon: str, now=None) -> dict:
    now_ts = _utc(now)
    symbol = str(symbol).upper()
    tf = _timeframe_key(market, horizon)
    start = now_ts - pd.Timedelta(days=HISTORY_DAYS.get(horizon, 900))
    raw = cache.get(market, symbol, tf, start=start, end=now_ts)
    closed = cache.closed_only(raw, market, horizon, now_ts)
    quality = _quality_assessment(closed, market, horizon, now_ts)
    model = db.model(market, symbol, horizon)
    drift = _drift_assessment(closed, model, horizon)
    multiplier = min(float(quality["data_multiplier"]), float(drift["drift_multiplier"]))
    return {
        "market": market,
        "symbol": symbol,
        "horizon": horizon,
        "checked_at": now_ts.isoformat(),
        **quality,
        **drift,
        "quality_drift_multiplier": multiplier,
    }


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS health_latest(
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  data_status TEXT NOT NULL,
  drift_status TEXT NOT NULL,
  quality_score REAL NOT NULL,
  drift_score REAL NOT NULL,
  size_multiplier REAL NOT NULL,
  last_bar TEXT,
  stale_hours REAL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(market,symbol,horizon)
);
CREATE TABLE IF NOT EXISTS health_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL,
  event_type TEXT NOT NULL,
  old_status TEXT,
  new_status TEXT,
  detail_json TEXT,
  created_at TEXT NOT NULL
);
"""


class DataQualityDriftMonitor:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.executescript(SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def save(self, result: dict):
        key = (result["market"], result["symbol"], result["horizon"])
        with self._c() as c:
            old = c.execute(
                "SELECT data_status,drift_status,size_multiplier FROM health_latest WHERE market=? AND symbol=? AND horizon=?",
                key,
            ).fetchone()
            c.execute("""
              INSERT INTO health_latest(
                market,symbol,horizon,checked_at,data_status,drift_status,quality_score,drift_score,
                size_multiplier,last_bar,stale_hours,payload_json
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(market,symbol,horizon) DO UPDATE SET
                checked_at=excluded.checked_at,data_status=excluded.data_status,drift_status=excluded.drift_status,
                quality_score=excluded.quality_score,drift_score=excluded.drift_score,
                size_multiplier=excluded.size_multiplier,last_bar=excluded.last_bar,
                stale_hours=excluded.stale_hours,payload_json=excluded.payload_json
            """, (
                *key, result["checked_at"], result["data_status"], result["drift_status"],
                float(result["quality_score"]), float(result["drift_score"]),
                float(result["quality_drift_multiplier"]), result.get("last_bar"),
                result.get("stale_hours"), _json(result),
            ))
            if old:
                old_pair = f"{old['data_status']}|{old['drift_status']}"
                new_pair = f"{result['data_status']}|{result['drift_status']}"
                materially_changed = abs(float(old["size_multiplier"]) - float(result["quality_drift_multiplier"])) >= 0.15
                if old_pair != new_pair or materially_changed:
                    c.execute("""
                      INSERT INTO health_events(market,symbol,horizon,event_type,old_status,new_status,detail_json,created_at)
                      VALUES(?,?,?,?,?,?,?,?)
                    """, (*key, "HEALTH_CHANGED", old_pair, new_pair, _json(result), result["checked_at"]))
            else:
                c.execute("""
                  INSERT INTO health_events(market,symbol,horizon,event_type,old_status,new_status,detail_json,created_at)
                  VALUES(?,?,?,?,?,?,?,?)
                """, (*key, "HEALTH_REGISTERED", None,
                       f"{result['data_status']}|{result['drift_status']}", _json(result), result["checked_at"]))

    def scan_all(self, db, cache, now=None) -> dict:
        checked = warnings = critical = drifted = 0
        errors = []
        for asset in db.assets():
            for horizon in _HORIZONS:
                market, symbol = asset["market"], asset["symbol"]
                if db.model(market, symbol, horizon) is None:
                    continue
                checked += 1
                try:
                    result = assess_pair(db, cache, market, symbol, horizon, now)
                    self.save(result)
                    warnings += int(result["data_status"] == "WARNING" or result["drift_status"] == "WATCH")
                    critical += int(result["data_status"] == "CRITICAL")
                    drifted += int(result["drift_status"] in ("DRIFT", "SEVERE"))
                except Exception as exc:
                    errors.append({
                        "market": market, "symbol": symbol, "horizon": horizon,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
        return {
            "status": "OK" if not errors else "PARTIAL",
            "checked": checked,
            "warnings": warnings,
            "critical_data": critical,
            "drifted": drifted,
            "errors": errors,
            "broker_order_api_calls": 0,
        }

    def latest_rows(self):
        with self._c() as c:
            rows = c.execute("""
              SELECT * FROM health_latest
              ORDER BY size_multiplier ASC, drift_score DESC, quality_score ASC, market, symbol, horizon
            """).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def recent_events(self, limit=100):
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM health_events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()]
