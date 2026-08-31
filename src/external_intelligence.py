from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .paths import data_dir

PUBLIC_PATH = Path("static") / "daily_external_intelligence.json"
PERSIST_DIR = Path(os.getenv("V6_PERSISTENT_DATA_DIR") or data_dir())
HISTORY_PATH = PERSIST_DIR / "daily_external_intelligence_history.jsonl"
MAX_AGE_SECONDS = 12 * 3600

NEGATIVE = {
    "crash", "selloff", "sell-off", "recession", "inflation", "tariff", "war", "attack",
    "sanction", "default", "bankruptcy", "fraud", "hack", "exploit", "liquidation", "outflow",
    "downgrade", "miss", "plunge", "slump", "fear", "risk-off", "hawkish", "tightening",
}
POSITIVE = {
    "rally", "surge", "beat", "upgrade", "approval", "inflow", "growth", "cooling inflation",
    "rate cut", "dovish", "record high", "breakout", "rebound", "optimism", "risk-on",
}
EVENT = {
    "fed", "fomc", "cpi", "pce", "payroll", "jobs report", "unemployment", "rate decision",
    "earnings", "guidance", "sec", "etf", "regulation", "tariff", "sanction", "war", "attack",
    "hack", "exploit", "bankruptcy", "liquidation",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _fetch(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "V6-Quant-Lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _google_news(query: str, limit: int = 30) -> list[dict]:
    # Limit the evidence window. Old headlines can still be repeated by an RSS
    # provider, so _headline_metrics applies a second timestamp decay below.
    q = urllib.parse.quote_plus(f"({query}) when:2d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    root = ET.fromstring(_fetch(url))
    out = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if title:
            source_node = item.find("source")
            out.append({
                "title": title,
                "source": ((source_node.text if source_node is not None else "") or "").strip(),
                "published_at": (item.findtext("pubDate") or "").strip(),
            })
    return out


def _yahoo_chart(symbol: str) -> dict:
    s = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d"
    raw = json.loads(_fetch(url).decode("utf-8"))
    result = (((raw.get("chart") or {}).get("result") or [None])[0] or {})
    quote = ((((result.get("indicators") or {}).get("quote") or [None])[0]) or {})
    closes = [float(x) for x in (quote.get("close") or []) if x is not None and math.isfinite(float(x))]
    if not closes:
        return {"symbol": symbol, "last": None, "change_pct": None}
    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else last
    change = (last / prev - 1.0) if prev else 0.0
    return {"symbol": symbol, "last": last, "change_pct": change}


def _headline_record(value) -> dict:
    if isinstance(value, dict):
        return {
            "title": str(value.get("title") or "").strip(),
            "source": str(value.get("source") or "").strip(),
            "published_at": str(value.get("published_at") or "").strip(),
        }
    return {"title": str(value or "").strip(), "source": "", "published_at": ""}


def _normalized_headline(title: str) -> str:
    text = re.sub(r"\s+-\s+[^-]{2,80}$", "", str(title or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _headline_age_hours(raw: str) -> float | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (_now() - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)
        except Exception:
            return None


def _headline_metrics(headlines: list) -> dict:
    if not headlines:
        return {
            "sentiment": 0.0, "event_risk": 0.0, "headline_count": 0,
            "raw_headline_count": 0, "duplicate_count": 0, "effective_headline_count": 0.0,
            "source_count": 0, "source_diversity": 0.0, "recency_coverage": 0.0,
            "evidence_quality": 0.0,
        }

    records = [_headline_record(value) for value in headlines]
    unique = []
    seen = set()
    for record in records:
        key = _normalized_headline(record["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)

    positive = negative = events = total_weight = timestamped_weight = 0.0
    sources = set()
    for record in unique:
        text = re.sub(r"\s+", " ", record["title"].lower())
        age_hours = _headline_age_hours(record["published_at"])
        # Unknown timestamps remain usable but never receive full confidence.
        weight = 0.50 if age_hours is None else math.exp(-math.log(2.0) * age_hours / 18.0)
        weight = _clamp(weight, 0.05, 1.0)
        total_weight += weight
        if age_hours is not None:
            timestamped_weight += weight
        positive += min(2, sum(1 for token in POSITIVE if token in text)) * weight
        negative += min(2, sum(1 for token in NEGATIVE if token in text)) * weight
        events += min(3, sum(1 for token in EVENT if token in text)) * weight
        if record["source"]:
            sources.add(record["source"].lower())

    total_hits = max(1.0, positive + negative)
    sentiment = _clamp((positive - negative) / total_hits, -1.0, 1.0)
    event_risk = _clamp(events / max(4.0, total_weight * 0.40), 0.0, 1.0)
    source_diversity = _clamp(len(sources) / max(3.0, math.sqrt(max(1, len(unique)))), 0.0, 1.0)
    recency_coverage = _clamp(timestamped_weight / max(total_weight, 1e-12), 0.0, 1.0)
    dedupe_quality = _clamp(len(unique) / max(1, len(records)), 0.0, 1.0)
    sample_quality = _clamp(total_weight / 8.0, 0.0, 1.0)
    evidence_quality = _clamp(
        0.30 * source_diversity + 0.25 * recency_coverage + 0.20 * dedupe_quality + 0.25 * sample_quality,
        0.0,
        1.0,
    )
    return {
        "sentiment": sentiment,
        "event_risk": event_risk,
        "headline_count": len(unique),
        "raw_headline_count": len(records),
        "duplicate_count": max(0, len(records) - len(unique)),
        "effective_headline_count": total_weight,
        "source_count": len(sources),
        "source_diversity": source_diversity,
        "recency_coverage": recency_coverage,
        "evidence_quality": evidence_quality,
    }


def _market_stress(market_data: dict) -> float:
    vix = market_data.get("VIX") or {}
    tnx = market_data.get("TNX") or {}
    dxy = market_data.get("DXY") or {}
    spx = market_data.get("SPX") or {}
    stress = 0.0
    vix_last = vix.get("last")
    if vix_last is not None:
        stress += _clamp((float(vix_last) - 16.0) / 24.0, 0.0, 1.0) * 0.45
    if float(tnx.get("change_pct") or 0.0) > 0.015:
        stress += 0.15
    if float(dxy.get("change_pct") or 0.0) > 0.006:
        stress += 0.15
    spx_chg = float(spx.get("change_pct") or 0.0)
    if spx_chg < -0.01:
        stress += min(0.25, abs(spx_chg) * 8.0)
    return _clamp(stress, 0.0, 1.0)


def _context(sentiment: float, event_risk: float, stress: float, confidence: float) -> dict:
    risk = _clamp(0.45 * stress + 0.35 * event_risk + 0.20 * max(0.0, -sentiment), 0.0, 1.0)
    if risk >= 0.72:
        regime = "RISK_OFF_HIGH"
        market_mult = 0.55
    elif risk >= 0.52:
        regime = "RISK_OFF"
        market_mult = 0.70
    elif risk >= 0.32:
        regime = "CAUTION"
        market_mult = 0.85
    else:
        regime = "NORMAL"
        market_mult = 1.00

    # This first version may only reduce risk. No external signal can increase
    # exposure above the model's normal 1.00x baseline.
    strategy = {
        "Trend MA": market_mult,
        "Momentum": _clamp(market_mult - (0.10 if risk >= 0.52 else 0.0), 0.50, 1.0),
        "Mean Reversion RSI": _clamp(market_mult + (0.10 if risk >= 0.52 else 0.0), 0.55, 1.0),
        "Breakout": _clamp(market_mult - (0.05 if event_risk >= 0.60 else 0.0), 0.50, 1.0),
    }
    return {
        "risk_regime": regime,
        "risk_score": risk,
        "sentiment_score": _clamp(sentiment, -1.0, 1.0),
        "event_risk": _clamp(event_risk, 0.0, 1.0),
        "market_stress": _clamp(stress, 0.0, 1.0),
        "confidence": _clamp(confidence, 0.0, 1.0),
        "market_multiplier": market_mult,
        "strategy_multipliers": strategy,
    }


def build_daily_external_intelligence() -> dict:
    errors: list[str] = []
    macro_headlines: list[dict] = []
    crypto_headlines: list[dict] = []
    try:
        macro_headlines = _google_news("Federal Reserve OR CPI OR inflation OR jobs OR Treasury OR stock market")
    except Exception as exc:
        errors.append(f"macro_news:{type(exc).__name__}")
    try:
        crypto_headlines = _google_news("Bitcoin OR Ethereum OR crypto ETF OR SEC crypto OR crypto hack")
    except Exception as exc:
        errors.append(f"crypto_news:{type(exc).__name__}")

    md: dict[str, dict] = {}
    for key, symbol in {"VIX": "^VIX", "TNX": "^TNX", "DXY": "DX-Y.NYB", "SPX": "^GSPC", "BTC": "BTC-USD"}.items():
        try:
            md[key] = _yahoo_chart(symbol)
        except Exception as exc:
            md[key] = {"symbol": symbol, "last": None, "change_pct": None}
            errors.append(f"market_{key}:{type(exc).__name__}")

    macro = _headline_metrics(macro_headlines)
    crypto = _headline_metrics(crypto_headlines)
    stress = _market_stress(md)
    market_coverage = _clamp(sum(1 for x in md.values() if x.get("last") is not None) / 5.0, 0.0, 1.0)
    headline_coverage = 0.50 * _clamp(macro["headline_count"] / 10.0, 0.0, 1.0) + 0.50 * _clamp(crypto["headline_count"] / 10.0, 0.0, 1.0)
    headline_quality = 0.50 * macro["evidence_quality"] + 0.50 * crypto["evidence_quality"]
    confidence = _clamp(0.55 * market_coverage + 0.20 * headline_coverage + 0.25 * headline_quality, 0.0, 1.0)

    stock_ctx = _context(macro["sentiment"], macro["event_risk"], stress, confidence)
    btc_chg = float((md.get("BTC") or {}).get("change_pct") or 0.0)
    crypto_stress = _clamp(stress * 0.55 + (0.25 if btc_chg < -0.03 else 0.0), 0.0, 1.0)
    crypto_sent = _clamp(0.35 * macro["sentiment"] + 0.65 * crypto["sentiment"], -1.0, 1.0)
    crypto_event = max(macro["event_risk"] * 0.45, crypto["event_risk"])
    crypto_ctx = _context(crypto_sent, crypto_event, crypto_stress, confidence)

    status = "AVAILABLE" if confidence >= 0.55 else "DEGRADED"
    if confidence < 0.25:
        # Fail-open: insufficient external data must never create a false risk signal.
        stock_ctx = _context(0.0, 0.0, 0.0, confidence)
        crypto_ctx = _context(0.0, 0.0, 0.0, confidence)
        status = "UNAVAILABLE"

    return {
        "generated_at": _iso(),
        "status": status,
        "scope": "PUBLIC_READ_ONLY_DAILY_EXTERNAL_INTELLIGENCE",
        "contains_secrets": False,
        "shadow_only": True,
        "can_increase_exposure": False,
        "broker_order_api_calls": 0,
        "refresh_policy_hours": 6,
        "sources": {
            "macro_news": "Google News RSS",
            "crypto_news": "Google News RSS",
            "market_context": "Yahoo Finance chart endpoint",
            "source_coverage": confidence,
            "market_data_coverage": market_coverage,
            "headline_coverage": headline_coverage,
            "headline_quality": headline_quality,
            "headline_window_hours": 48,
            "errors": errors[:20],
        },
        "market_data": md,
        "headline_summary": {"macro": macro, "crypto": crypto},
        "markets": {"stock": stock_ctx, "crypto": crypto_ctx, "twstock": stock_ctx},
    }


def write_daily_external_intelligence(path: Path | None = None) -> dict:
    payload = build_daily_external_intelligence()
    target = path or PUBLIC_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(target)
    try:
        PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
    except Exception:
        pass
    return payload


def read_daily_external_intelligence(path: Path | None = None) -> dict | None:
    target = path or PUBLIC_PATH
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def external_intelligence_assessment(market: str, strategy: str, path: Path | None = None) -> dict:
    neutral = {
        "external_intelligence_multiplier": 1.0,
        "external_market_multiplier": 1.0,
        "external_strategy_multiplier": 1.0,
        "external_risk_regime": "UNAVAILABLE",
        "external_risk_score": 0.0,
        "external_sentiment_score": 0.0,
        "external_event_risk": 0.0,
        "external_confidence": 0.0,
        "external_status": "UNAVAILABLE",
        "external_generated_at": None,
    }
    snap = read_daily_external_intelligence(path)
    if not snap:
        return neutral
    try:
        generated = datetime.fromisoformat(str(snap.get("generated_at") or "").replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age = (_now() - generated.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return neutral
    if age > MAX_AGE_SECONDS or str(snap.get("status") or "") == "UNAVAILABLE":
        return neutral

    ctx = ((snap.get("markets") or {}).get(market) or {})
    market_mult = _clamp(float(ctx.get("market_multiplier") or 1.0), 0.0, 1.0)
    strategy_mult = _clamp(float((ctx.get("strategy_multipliers") or {}).get(strategy, market_mult)), 0.0, 1.0)
    effective = min(market_mult, strategy_mult)
    return {
        "external_intelligence_multiplier": effective,
        "external_market_multiplier": market_mult,
        "external_strategy_multiplier": strategy_mult,
        "external_risk_regime": str(ctx.get("risk_regime") or "UNKNOWN"),
        "external_risk_score": float(ctx.get("risk_score") or 0.0),
        "external_sentiment_score": float(ctx.get("sentiment_score") or 0.0),
        "external_event_risk": float(ctx.get("event_risk") or 0.0),
        "external_confidence": float(ctx.get("confidence") or 0.0),
        "external_status": str(snap.get("status") or "UNKNOWN"),
        "external_generated_at": snap.get("generated_at"),
    }
