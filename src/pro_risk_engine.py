from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .paths import data_dir

SNAPSHOT_PATH = Path(data_dir()) / "professional_risk_snapshot.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market_from_account(account_id: str) -> str:
    aid = str(account_id or "")
    return aid.rsplit("_", 1)[0] if "_" in aid else aid


def _short_timeframe(market: str) -> str:
    return "1Hour" if market == "stock" else "1h"


def _account_equity(db, account_id: str) -> float:
    acct = db.account(account_id) or {}
    cash = float(acct.get("cash") or 0.0)
    marks = db.marks(account_id)
    gross = 0.0
    for p in db.positions(account_id):
        sym = str(p.get("symbol") or "").upper()
        mark = float(marks.get(sym, p.get("avg_entry") or 0.0) or 0.0)
        gross += abs(float(p.get("qty") or 0.0)) * mark
    return max(0.0, cash + gross)


def _position_rows(db) -> list[dict]:
    rows: list[dict] = []
    for p in db.positions():
        aid = str(p.get("account_id") or "")
        market = _market_from_account(aid)
        symbol = str(p.get("symbol") or "").upper()
        marks = db.marks(aid)
        entry = float(p.get("avg_entry") or 0.0)
        mark = float(marks.get(symbol, entry) or entry)
        qty = abs(float(p.get("qty") or 0.0))
        notional = qty * mark
        stop = float(p.get("stop_price") or 0.0)
        if mark > 0 and stop > 0 and stop < mark:
            stop_risk_amount = qty * (mark - stop)
        else:
            stop_risk_amount = 0.0
        rows.append({
            "account_id": aid,
            "market": market,
            "horizon": str(p.get("horizon") or ""),
            "symbol": symbol,
            "strategy": p.get("strategy"),
            "entry_price": entry,
            "mark_price": mark,
            "notional": notional,
            "stop_price": stop if stop > 0 else None,
            "stop_risk_amount": stop_risk_amount,
            "leverage": float(p.get("leverage_at_entry") or 1.0),
        })
    return rows


def _return_series(cache, market: str, symbol: str, max_bars: int = 240) -> pd.Series:
    try:
        df = cache.get(market, symbol, _short_timeframe(market))
        if df is None or df.empty:
            return pd.Series(dtype=float)
        s = pd.to_numeric(df.close, errors="coerce").pct_change().replace([np.inf, -np.inf], np.nan).dropna().tail(max_bars)
        s.name = symbol
        return s
    except Exception:
        return pd.Series(dtype=float)


def _correlation_rows(cache, positions: list[dict]) -> list[dict]:
    rows: list[dict] = []
    by_market: dict[str, list[str]] = {}
    for p in positions:
        by_market.setdefault(p["market"], [])
        if p["symbol"] not in by_market[p["market"]]:
            by_market[p["market"]].append(p["symbol"])

    for market, symbols in by_market.items():
        series = {s: _return_series(cache, market, s) for s in symbols}
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                a, b = symbols[i], symbols[j]
                x, y = series[a], series[b]
                if x.empty or y.empty:
                    continue
                pair = pd.concat([x, y], axis=1, join="inner").dropna()
                if len(pair) < 40:
                    continue
                corr = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
                if np.isfinite(corr):
                    rows.append({"market": market, "symbol_a": a, "symbol_b": b, "correlation": corr, "samples": int(len(pair))})
    return rows


def _risk_status(score: float) -> tuple[str, float]:
    if score >= 75:
        return "CRITICAL", 0.40
    if score >= 55:
        return "HIGH", 0.65
    if score >= 30:
        return "MEDIUM", 0.85
    return "LOW", 1.00


def _group_risk(db, group_name: str, positions: list[dict], correlations: list[dict]) -> dict:
    account_ids = sorted({p["account_id"] for p in positions})
    if group_name == "GLOBAL":
        all_accounts = db.accounts()
    else:
        all_accounts = [a for a in db.accounts() if str(a.get("market")) == group_name]
    base_accounts = [str(a.get("account_id")) for a in all_accounts]
    equity = sum(_account_equity(db, aid) for aid in base_accounts)
    if equity <= 0:
        equity = sum(float(a.get("initial_equity") or 0.0) for a in all_accounts)

    gross = float(sum(p["notional"] for p in positions))
    stop_risk = float(sum(p["stop_risk_amount"] for p in positions))
    gross_ratio = gross / equity if equity > 0 else 0.0
    stop_risk_pct = stop_risk / equity if equity > 0 else 0.0

    if gross > 0:
        weights = [p["notional"] / gross for p in positions]
        max_weight = max(weights)
        hhi = sum(w * w for w in weights)
        effective_positions = 1.0 / hhi if hhi > 0 else 0.0
    else:
        max_weight = 0.0
        effective_positions = 0.0

    counts = Counter(p["symbol"] for p in positions)
    duplicate_symbols = sum(1 for _, n in counts.items() if n > 1)

    if group_name == "GLOBAL":
        corr_rows = correlations
    else:
        corr_rows = [r for r in correlations if r["market"] == group_name]
    max_corr = max([float(r["correlation"]) for r in corr_rows], default=0.0)
    high_corr_pairs = sum(1 for r in corr_rows if float(r["correlation"]) >= 0.75)

    # This is a transparent diagnostic score, not a probability of loss.
    score = 0.0
    score += min(25.0, gross_ratio / 1.00 * 25.0)
    score += min(30.0, stop_risk_pct / 0.08 * 30.0)
    if len(positions) >= 2:
        score += min(15.0, max(0.0, max_weight - 0.35) / 0.45 * 15.0)
    score += min(15.0, duplicate_symbols * 7.5)
    if max_corr >= 0.90:
        score += 15.0
    elif max_corr >= 0.75:
        score += 10.0
    elif max_corr >= 0.60:
        score += 5.0
    score = float(np.clip(score, 0.0, 100.0))
    status, multiplier = _risk_status(score)

    return {
        "group": group_name,
        "accounts": len(base_accounts),
        "positions": len(positions),
        "unique_symbols": len(counts),
        "equity": float(equity),
        "gross_exposure": gross,
        "gross_ratio": gross_ratio,
        "stop_risk_amount": stop_risk,
        "stop_risk_pct": stop_risk_pct,
        "max_position_weight": max_weight,
        "effective_positions": effective_positions,
        "duplicate_symbols": duplicate_symbols,
        "max_pair_correlation": max_corr,
        "high_corr_pairs": high_corr_pairs,
        "risk_score": score,
        "risk_status": status,
        "shadow_risk_multiplier": multiplier,
        "shadow_only": True,
    }


def portfolio_risk_snapshot(db, cache) -> dict:
    positions = _position_rows(db)
    correlations = _correlation_rows(cache, positions)
    groups = [_group_risk(db, "GLOBAL", positions, correlations)]
    for market in ("crypto", "stock", "twstock"):
        groups.append(_group_risk(db, market, [p for p in positions if p["market"] == market], correlations))

    gross = sum(p["notional"] for p in positions)
    for p in positions:
        p["global_weight"] = p["notional"] / gross if gross > 0 else 0.0

    return {
        "generated_at": _now_iso(),
        "groups": groups,
        "positions": positions,
        "correlations": correlations,
        "shadow_only": True,
    }


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    adj = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return ((centre - adj) / denom, (centre + adj) / denom)


def _loss_streak(values: list[float]) -> int:
    best = cur = 0
    for v in values:
        if v < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _health_row(g: pd.DataFrame, keys: dict) -> dict:
    g = g.sort_values("_exit_time").tail(30).copy()
    n = len(g)
    rets = pd.to_numeric(g["return_pct"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    pnls = pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    wins = int((pnls > 0).sum())
    losses_amt = float(-pnls[pnls < 0].sum())
    wins_amt = float(pnls[pnls > 0].sum())
    pf = wins_amt / losses_amt if losses_amt > 0 else (float("inf") if wins_amt > 0 else np.nan)

    age = np.arange(n - 1, -1, -1, dtype=float)
    weights = np.power(0.5, age / 8.0)
    weights = weights / weights.sum() if weights.sum() > 0 else np.ones(n) / max(n, 1)
    weighted_avg_return = float(np.sum(rets * weights)) if n else 0.0
    weighted_win_rate = float(np.sum((pnls > 0).astype(float) * weights)) if n else 0.0
    avg_return = float(np.mean(rets)) if n else 0.0
    median_return = float(np.median(rets)) if n else 0.0

    recent_n = min(5, n)
    recent_avg = float(np.mean(rets[-recent_n:])) if recent_n else 0.0
    prior = rets[max(0, n - recent_n - 10): n - recent_n]
    prior_avg = float(np.mean(prior)) if len(prior) >= 3 else np.nan
    deterioration = recent_avg - prior_avg if np.isfinite(prior_avg) else np.nan
    streak = _loss_streak(rets.tolist())
    ci_low, ci_high = _wilson_interval(wins, n)

    pf_component = 0.0
    if np.isfinite(pf):
        pf_component = float(np.clip(math.log(max(pf, 1e-6)), -1.0, 1.0) * 15.0)
    elif wins_amt > 0:
        pf_component = 15.0
    raw = 50.0
    raw += float(np.clip((weighted_win_rate - 0.50) / 0.30, -1, 1) * 20.0)
    raw += pf_component
    raw += float(np.clip(weighted_avg_return / 0.03, -1, 1) * 15.0)
    if np.isfinite(deterioration):
        raw += float(np.clip(deterioration / 0.02, -1, 1) * 10.0)
    credibility = min(1.0, n / 20.0)
    score = float(np.clip(50.0 + (raw - 50.0) * credibility, 0.0, 100.0))

    failure_votes = 0
    if n >= 5 and weighted_win_rate < 0.35:
        failure_votes += 1
    if n >= 5 and np.isfinite(pf) and pf < 0.80:
        failure_votes += 1
    if n >= 5 and weighted_avg_return < 0:
        failure_votes += 1
    if n >= 8 and streak >= 4:
        failure_votes += 1
    if n >= 8 and np.isfinite(deterioration) and deterioration <= -0.02:
        failure_votes += 1

    if n < 5:
        state, mult = "LEARNING", 1.00
    elif n >= 20 and failure_votes >= 4:
        state, mult = "PAUSE_CANDIDATE", 0.25
    elif n >= 10 and failure_votes >= 3:
        state, mult = "SHADOW_ONLY_CANDIDATE", 0.50
    elif failure_votes >= 1:
        state, mult = "WATCH", 0.80
    else:
        state, mult = "NORMAL", 1.00

    return {
        **keys,
        "samples": int(n),
        "weighted_win_rate": weighted_win_rate,
        "win_rate_ci_low": float(ci_low),
        "win_rate_ci_high": float(ci_high),
        "profit_factor": float(pf) if np.isfinite(pf) else None,
        "profit_factor_infinite": bool(np.isinf(pf)),
        "avg_return": avg_return,
        "median_return": median_return,
        "weighted_avg_return": weighted_avg_return,
        "recent_avg_return": recent_avg,
        "prior_avg_return": float(prior_avg) if np.isfinite(prior_avg) else None,
        "deterioration": float(deterioration) if np.isfinite(deterioration) else None,
        "max_loss_streak": int(streak),
        "health_score": score,
        "failure_votes": int(failure_votes),
        "state": state,
        "shadow_weight_multiplier": mult,
        "shadow_only": True,
    }


def strategy_health_snapshot(db) -> dict:
    trades = pd.DataFrame(db.recent_trades(5000))
    if trades.empty:
        return {"generated_at": _now_iso(), "strategies": [], "regimes": [], "shadow_only": True}

    trades["market"] = trades["account_id"].map(_market_from_account)
    trades["_exit_time"] = pd.to_datetime(trades["exit_bar"], utc=True, errors="coerce")
    trades = trades.dropna(subset=["_exit_time"])

    strategies = []
    for (market, horizon, strategy), g in trades.groupby(["market", "horizon", "strategy"], dropna=False):
        strategies.append(_health_row(g, {"market": market, "horizon": horizon, "strategy": strategy or "UNKNOWN"}))

    regimes = []
    for (market, horizon, strategy, regime), g in trades.groupby(["market", "horizon", "strategy", "regime_entry"], dropna=False):
        regimes.append(_health_row(g, {
            "market": market,
            "horizon": horizon,
            "strategy": strategy or "UNKNOWN",
            "regime": regime or "UNKNOWN",
        }))

    strategies.sort(key=lambda r: (r["health_score"], -r["samples"]))
    regimes.sort(key=lambda r: (r["health_score"], -r["samples"]))
    return {"generated_at": _now_iso(), "strategies": strategies, "regimes": regimes, "shadow_only": True}


def build_professional_risk_snapshot(db, cache) -> dict:
    return {
        "generated_at": _now_iso(),
        "portfolio": portfolio_risk_snapshot(db, cache),
        "strategy_health": strategy_health_snapshot(db),
        "shadow_only": True,
        "broker_order_api_calls": 0,
    }


def write_professional_risk_snapshot(db, cache, path: Path | None = None) -> dict:
    payload = build_professional_risk_snapshot(db, cache)
    target = path or SNAPSHOT_PATH
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(target)
    return payload


def read_professional_risk_snapshot(path: Path | None = None) -> dict | None:
    target = path or SNAPSHOT_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
