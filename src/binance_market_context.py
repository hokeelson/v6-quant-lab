from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PUBLIC_PATH = Path("static") / "binance_market_context.json"
MAX_AGE_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _float(value, default=None):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _get_json(base: str, path: str, params: dict | None = None, timeout: int = 8):
    query = urllib.parse.urlencode(params or {})
    url = f"{base}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "V6-Quant-Lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _spot_depth(symbol: str) -> dict:
    raw = _get_json(
        "https://api.binance.com",
        "/api/v3/depth",
        {"symbol": symbol, "limit": 20},
    )
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    bid_notional = sum(float(p) * float(q) for p, q in bids)
    ask_notional = sum(float(p) * float(q) for p, q in asks)
    total = bid_notional + ask_notional
    bid_share = bid_notional / total if total > 0 else 0.5
    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None
    mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None
    spread_bps = ((best_ask - best_bid) / mid * 10000.0) if mid and mid > 0 else None
    return {
        "bid_notional_top20": bid_notional,
        "ask_notional_top20": ask_notional,
        "bid_share_top20": bid_share,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": spread_bps,
    }


def _futures_premium(symbol: str) -> dict:
    raw = _get_json(
        "https://fapi.binance.com",
        "/fapi/v1/premiumIndex",
        {"symbol": symbol},
    )
    return {
        "mark_price": _float(raw.get("markPrice")),
        "index_price": _float(raw.get("indexPrice")),
        "last_funding_rate": _float(raw.get("lastFundingRate")),
        "next_funding_time": raw.get("nextFundingTime"),
    }


def _futures_open_interest(symbol: str) -> dict:
    raw = _get_json(
        "https://fapi.binance.com",
        "/fapi/v1/openInterest",
        {"symbol": symbol},
    )
    return {
        "open_interest": _float(raw.get("openInterest")),
        "open_interest_time": raw.get("time"),
    }


def _futures_long_short(symbol: str) -> dict:
    raw = _get_json(
        "https://fapi.binance.com",
        "/futures/data/globalLongShortAccountRatio",
        {"symbol": symbol, "period": "5m", "limit": 1},
    )
    row = raw[-1] if isinstance(raw, list) and raw else {}
    return {
        "long_short_ratio": _float(row.get("longShortRatio")),
        "long_account": _float(row.get("longAccount")),
        "short_account": _float(row.get("shortAccount")),
        "long_short_time": row.get("timestamp"),
    }


def _risk_from_row(row: dict) -> tuple[float, list[str]]:
    risk = 0.0
    reasons: list[str] = []

    funding = abs(float(row.get("last_funding_rate") or 0.0))
    if funding >= 0.003:
        risk += 0.35
        reasons.append("funding_extreme")
    elif funding >= 0.0015:
        risk += 0.22
        reasons.append("funding_high")
    elif funding >= 0.0008:
        risk += 0.12
        reasons.append("funding_elevated")

    ratio = row.get("long_short_ratio")
    if ratio is not None and ratio > 0:
        ratio = float(ratio)
        if ratio >= 3.0 or ratio <= 1 / 3:
            risk += 0.35
            reasons.append("positioning_extreme")
        elif ratio >= 2.0 or ratio <= 0.5:
            risk += 0.22
            reasons.append("positioning_crowded")
        elif ratio >= 1.6 or ratio <= 0.625:
            risk += 0.12
            reasons.append("positioning_skewed")

    bid_share = row.get("bid_share_top20")
    if bid_share is not None:
        imbalance = abs(float(bid_share) - 0.5) * 2.0
        if imbalance >= 0.70:
            risk += 0.20
            reasons.append("orderbook_extreme")
        elif imbalance >= 0.50:
            risk += 0.12
            reasons.append("orderbook_imbalanced")
        elif imbalance >= 0.35:
            risk += 0.06
            reasons.append("orderbook_skewed")

    spread = row.get("spread_bps")
    if spread is not None:
        spread = float(spread)
        if spread >= 25:
            risk += 0.20
            reasons.append("spread_very_wide")
        elif spread >= 12:
            risk += 0.12
            reasons.append("spread_wide")
        elif spread >= 6:
            risk += 0.06
            reasons.append("spread_elevated")

    risk = _clamp(risk, 0.0, 1.0)
    if risk >= 0.70:
        mult = 0.55
        state = "HIGH_RISK"
    elif risk >= 0.50:
        mult = 0.70
        state = "CAUTION"
    elif risk >= 0.30:
        mult = 0.85
        state = "WATCH"
    else:
        mult = 1.00
        state = "NORMAL"

    return risk, reasons + [state]


def fetch_symbol_context(symbol: str) -> dict:
    symbol = str(symbol or "").upper()
    row = {
        "symbol": symbol,
        "generated_at": _iso(),
        "spot_depth_available": False,
        "futures_available": False,
        "errors": [],
    }

    try:
        row.update(_spot_depth(symbol))
        row["spot_depth_available"] = True
    except Exception as exc:
        row["errors"].append(f"spot_depth:{type(exc).__name__}")

    futures_parts = 0
    for name, fn in (
        ("premium", _futures_premium),
        ("open_interest", _futures_open_interest),
        ("long_short", _futures_long_short),
    ):
        try:
            row.update(fn(symbol))
            futures_parts += 1
        except Exception as exc:
            row["errors"].append(f"{name}:{type(exc).__name__}")
    row["futures_available"] = futures_parts > 0
    row["futures_coverage"] = futures_parts / 3.0

    risk, reasons = _risk_from_row(row)
    row["risk_score"] = risk
    row["size_multiplier"] = 1.0 if not row["spot_depth_available"] else (
        0.55 if risk >= 0.70 else 0.70 if risk >= 0.50 else 0.85 if risk >= 0.30 else 1.0
    )
    row["risk_state"] = reasons[-1] if reasons else "NORMAL"
    row["reasons"] = reasons[:-1] if reasons else []
    return row


def write_snapshot(symbols: list[str], path: Path | None = None) -> dict:
    rows = []
    for symbol in sorted({str(s).upper() for s in symbols if s}):
        rows.append(fetch_symbol_context(symbol))

    available = sum(1 for row in rows if row.get("spot_depth_available"))
    futures = sum(1 for row in rows if row.get("futures_available"))
    payload = {
        "generated_at": _iso(),
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "scope": "PUBLIC_READ_ONLY_BINANCE_MARKET_CONTEXT",
        "contains_secrets": False,
        "read_only": True,
        "can_increase_exposure": False,
        "broker_order_api_calls": 0,
        "refresh_seconds": 60,
        "symbols": len(rows),
        "spot_depth_coverage": available / max(1, len(rows)),
        "futures_coverage": futures / max(1, len(rows)),
        "rows": rows,
    }
    target = path or PUBLIC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(target)
    return payload


def read_snapshot(path: Path | None = None) -> dict | None:
    target = path or PUBLIC_PATH
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def binance_market_context_assessment(symbol: str, path: Path | None = None) -> dict:
    neutral = {
        "binance_context_multiplier": 1.0,
        "binance_context_status": "UNAVAILABLE",
        "binance_context_risk_score": 0.0,
        "binance_context_risk_state": "UNAVAILABLE",
        "binance_context_reasons": [],
        "binance_funding_rate": None,
        "binance_open_interest": None,
        "binance_long_short_ratio": None,
        "binance_bid_share_top20": None,
        "binance_spread_bps": None,
        "binance_context_generated_at": None,
    }
    snap = read_snapshot(path)
    if not snap or str(snap.get("status") or "") == "UNAVAILABLE":
        return neutral

    try:
        dt = datetime.fromisoformat(str(snap.get("generated_at") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (_now() - dt.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return neutral
    if age > MAX_AGE_SECONDS:
        return neutral

    symbol = str(symbol or "").upper()
    row = next((r for r in (snap.get("rows") or []) if str(r.get("symbol") or "").upper() == symbol), None)
    if not row or not row.get("spot_depth_available"):
        return neutral

    return {
        "binance_context_multiplier": _clamp(float(row.get("size_multiplier") or 1.0), 0.0, 1.0),
        "binance_context_status": "AVAILABLE",
        "binance_context_risk_score": float(row.get("risk_score") or 0.0),
        "binance_context_risk_state": str(row.get("risk_state") or "UNKNOWN"),
        "binance_context_reasons": list(row.get("reasons") or []),
        "binance_funding_rate": row.get("last_funding_rate"),
        "binance_open_interest": row.get("open_interest"),
        "binance_long_short_ratio": row.get("long_short_ratio"),
        "binance_bid_share_top20": row.get("bid_share_top20"),
        "binance_spread_bps": row.get("spread_bps"),
        "binance_context_generated_at": snap.get("generated_at"),
    }
