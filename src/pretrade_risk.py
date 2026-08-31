from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .external_intelligence import external_intelligence_assessment
from .paths import data_dir
from .pretrade_batch_overlay import apply_pretrade_batch_overlay

SNAPSHOT_PATH = Path(data_dir()) / "pretrade_risk_snapshot.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _market_from_account(account_id: str) -> str:
    aid = str(account_id or "")
    return aid.rsplit("_", 1)[0] if "_" in aid else aid


def _short_tf(market: str) -> str:
    return "1Hour" if market == "stock" else "1h"


def _returns(cache, market: str, symbol: str, max_bars: int = 240):
    try:
        df = cache.get(market, symbol, _short_tf(market))
        if df is None or df.empty:
            return pd.Series(dtype=float)
        return pd.to_numeric(df.close, errors="coerce").pct_change().replace([np.inf, -np.inf], np.nan).dropna().tail(max_bars)
    except Exception:
        return pd.Series(dtype=float)


def _corr(cache, market: str, a: str, b: str):
    if a == b:
        return 1.0, 999
    x = _returns(cache, market, a)
    y = _returns(cache, market, b)
    if x.empty or y.empty:
        return None, 0
    pair = pd.concat([x.rename("a"), y.rename("b")], axis=1, join="inner").dropna()
    if len(pair) < 40:
        return None, int(len(pair))
    c = float(pair.a.corr(pair.b))
    return (c if np.isfinite(c) else None), int(len(pair))


def _market_equity(db, market: str) -> float:
    equity = 0.0
    for acct in db.accounts():
        if str(acct.get("market")) != market:
            continue
        aid = str(acct.get("account_id"))
        cash = float(acct.get("cash") or 0.0)
        marks = db.marks(aid)
        gross = 0.0
        for p in db.positions(aid):
            sym = str(p.get("symbol") or "").upper()
            px = float(marks.get(sym, p.get("avg_entry") or 0.0) or 0.0)
            gross += abs(float(p.get("qty") or 0.0)) * px
        equity += cash + gross
    return max(0.0, equity)


def _market_positions(db, market: str) -> list[dict]:
    out = []
    for p in db.positions():
        aid = str(p.get("account_id") or "")
        if _market_from_account(aid) != market:
            continue
        symbol = str(p.get("symbol") or "").upper()
        marks = db.marks(aid)
        px = float(marks.get(symbol, p.get("avg_entry") or 0.0) or 0.0)
        out.append({
            "account_id": aid,
            "symbol": symbol,
            "notional": abs(float(p.get("qty") or 0.0)) * px,
        })
    return out


def build_pretrade_risk_snapshot(db, cache) -> dict:
    latest = {}
    for d in db.recent_decisions(5000):
        market = str(d.get("market") or "")
        symbol = str(d.get("symbol") or "").upper()
        horizon = str(d.get("horizon") or "")
        key = (market, symbol, horizon)
        if not market or not symbol or key in latest:
            continue
        latest[key] = d

    rows = []
    for (market, symbol, horizon), d in latest.items():
        if str(d.get("action") or "").upper() != "ENTER":
            continue
        requested = max(0.0, float(d.get("requested_notional") or 0.0))
        if requested <= 0:
            continue
        positions = _market_positions(db, market)
        equity = _market_equity(db, market)
        gross = float(sum(p["notional"] for p in positions))
        projected_ratio = (gross + requested) / equity if equity > 0 else 0.0
        same_symbol = [p for p in positions if p["symbol"] == symbol]
        duplicate = len(same_symbol) > 0

        corr_rows = []
        for p in positions:
            c, samples = _corr(cache, market, symbol, p["symbol"])
            if c is not None:
                corr_rows.append({"symbol": p["symbol"], "correlation": c, "samples": samples})
        max_corr = max([x["correlation"] for x in corr_rows], default=0.0)
        most_correlated = max(corr_rows, key=lambda x: x["correlation"], default=None)

        flags = []
        score = 0
        if projected_ratio > 1.00:
            flags.append("市場總曝險超過100%")
            score += 45
        elif projected_ratio > 0.75:
            flags.append("市場總曝險偏高")
            score += 25
        elif projected_ratio > 0.50:
            score += 10
        if duplicate:
            flags.append("同標的跨週期重複曝險")
            score += 25
        if max_corr >= 0.90:
            flags.append("與現有持倉高度相關")
            score += 30
        elif max_corr >= 0.75:
            flags.append("與現有持倉相關性偏高")
            score += 20
        elif max_corr >= 0.60:
            score += 8

        score = min(100, score)
        if score >= 60:
            verdict, multiplier = "BLOCK_CANDIDATE", 0.50
        elif score >= 30:
            verdict, multiplier = "CAUTION", 0.75
        else:
            verdict, multiplier = "ALLOW", 1.00

        strategy = str(d.get("strategy") or "")
        external = external_intelligence_assessment(market, strategy)
        external_mult = float(external.get("external_intelligence_multiplier", 1.0) or 1.0)
        if external_mult < 0.999999:
            flags.append(f"外部情報風險:{external.get('external_risk_regime', 'UNKNOWN')}")
        multiplier = min(multiplier, external_mult)

        rows.append({
            "market": market,
            "symbol": symbol,
            "horizon": horizon,
            "strategy": d.get("strategy"),
            "regime": d.get("regime"),
            "decision_time": d.get("bar_time"),
            "trade_confidence": float(d.get("confidence") or 0.0),
            "stop_distance": float(d.get("stop_distance") or 0.0),
            "target_distance": float(d.get("target_distance") or 0.0),
            "requested_notional": requested,
            "market_equity": equity,
            "current_gross": gross,
            "projected_gross_ratio": projected_ratio,
            "duplicate_symbol": duplicate,
            "max_correlation": float(max_corr),
            "most_correlated_symbol": most_correlated.get("symbol") if most_correlated else None,
            "risk_score": score,
            "verdict": verdict,
            "shadow_size_multiplier": multiplier,
            "external_intelligence_multiplier": external_mult,
            "external_market_multiplier": external.get("external_market_multiplier"),
            "external_strategy_multiplier": external.get("external_strategy_multiplier"),
            "external_risk_regime": external.get("external_risk_regime"),
            "external_risk_score": external.get("external_risk_score"),
            "external_sentiment_score": external.get("external_sentiment_score"),
            "external_event_risk": external.get("external_event_risk"),
            "external_confidence": external.get("external_confidence"),
            "external_status": external.get("external_status"),
            "external_generated_at": external.get("external_generated_at"),
            "flags": " / ".join(flags) if flags else "無明顯組合衝突",
            "shadow_only": True,
        })

    rows = apply_pretrade_batch_overlay(db, cache, rows, _corr)
    rows.sort(key=lambda r: ((r.get("batch_ev_rank") or 999999), -float(r.get("trade_confidence") or 0.0)))
    return {
        "generated_at": _now_iso(),
        "candidates": rows,
        "shadow_only": True,
        "batch_portfolio_ev_active": True,
        "dynamic_regime_allocation_active": True,
        "daily_external_intelligence_active": True,
        "broker_order_api_calls": 0,
    }


def write_pretrade_risk_snapshot(db, cache, path: Path | None = None) -> dict:
    payload = build_pretrade_risk_snapshot(db, cache)
    target = path or SNAPSHOT_PATH
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(target)
    return payload


def read_pretrade_risk_snapshot(path: Path | None = None) -> dict | None:
    target = path or SNAPSHOT_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
