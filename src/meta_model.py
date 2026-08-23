from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .realtime_layer import RealtimeDB


FEATURE_NAMES = [
    "trade_confidence",
    "model_confidence",
    "signal_strength",
    "regime_score",
    "oos_score",
    "vol_quality",
    "atr_quality",
    "is_short",
    "is_medium",
    "is_crypto",
    "is_stock",
    "strat_trend",
    "strat_momentum",
    "strat_meanrev",
    "strat_breakout",
]


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _clip01(x):
    return float(np.clip(_f(x), 0.0, 1.0))


def _diag(row: dict) -> dict:
    raw = row.get("diagnostics")
    if isinstance(raw, dict):
        return raw
    raw = row.get("diagnostics_json")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _features(decision: dict) -> np.ndarray:
    d = _diag(decision)
    horizon = str(decision.get("horizon") or "")
    market = str(decision.get("market") or "")
    strategy = str(decision.get("strategy") or "")
    atr_pct = _f(decision.get("atr_pct"), 0.03)
    return np.asarray([
        _clip01(_f(decision.get("confidence"), 50.0) / 100.0),
        _clip01(_f(d.get("model_confidence"), 50.0) / 100.0),
        _clip01(_f(d.get("signal_strength"), 50.0) / 100.0),
        _clip01(_f(d.get("regime_score"), 50.0) / 100.0),
        _clip01(_f(d.get("oos_score"), 50.0) / 100.0),
        _clip01(_f(d.get("vol_quality"), 50.0) / 100.0),
        _clip01(1.0 - atr_pct / 0.10),
        1.0 if horizon == "short" else 0.0,
        1.0 if horizon == "medium" else 0.0,
        1.0 if market == "crypto" else 0.0,
        1.0 if market == "stock" else 0.0,
        1.0 if strategy == "Trend MA" else 0.0,
        1.0 if strategy == "Momentum" else 0.0,
        1.0 if "Mean Reversion" in strategy or "MeanRev" in strategy else 0.0,
        1.0 if strategy == "Breakout" else 0.0,
    ], dtype=float)


def _heuristic_probability(decision: dict) -> float:
    x = _features(decision)
    # Only alpha/model-quality inputs drive cold-start probability. Portfolio risk
    # and strategy health remain separate sizing layers to avoid double-counting.
    core = (
        0.25 * x[0] +
        0.20 * x[1] +
        0.20 * x[2] +
        0.15 * x[3] +
        0.12 * x[4] +
        0.05 * x[5] +
        0.03 * x[6]
    )
    return float(np.clip(core, 0.05, 0.95))


def _ts(value):
    try:
        t = pd.Timestamp(value)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    except Exception:
        return None


def _training_rows(db, limit=5000):
    decisions = db.recent_decisions(limit)
    by_key = {}
    for d in decisions:
        if str(d.get("action") or "").upper() != "ENTER":
            continue
        key = (str(d.get("account_id") or ""), str(d.get("symbol") or "").upper())
        t = _ts(d.get("bar_time"))
        if t is None:
            continue
        by_key.setdefault(key, []).append((t, d))
    for rows in by_key.values():
        rows.sort(key=lambda z: z[0])

    out = []
    for tr in db.recent_trades(limit):
        entry = _ts(tr.get("entry_bar"))
        exit_t = _ts(tr.get("exit_bar"))
        if entry is None or exit_t is None:
            continue
        key = (str(tr.get("account_id") or ""), str(tr.get("symbol") or "").upper())
        candidates = by_key.get(key) or []
        prior = [d for t, d in candidates if t < entry]
        if not prior:
            continue
        d = prior[-1]
        x = _features(d)
        y = 1.0 if _f(tr.get("return_pct"), 0.0) > 0 else 0.0
        out.append((exit_t, x, y))
    out.sort(key=lambda z: z[0])
    return out


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _fit_logistic(X: np.ndarray, y: np.ndarray, l2=0.40, steps=700, lr=0.08):
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd = np.where(sd < 1e-6, 1.0, sd)
    Z = (X - mu) / sd
    A = np.column_stack([np.ones(len(Z)), Z])
    w = np.zeros(A.shape[1], dtype=float)
    for _ in range(steps):
        p = _sigmoid(A @ w)
        grad = (A.T @ (p - y)) / max(1, len(y))
        reg = np.r_[0.0, l2 * w[1:]]
        w -= lr * (grad + reg)
    return {"mu": mu, "sd": sd, "w": w}


def _predict(model, X):
    Z = (X - model["mu"]) / model["sd"]
    A = np.column_stack([np.ones(len(Z)), Z]) if Z.ndim == 2 else np.r_[1.0, Z]
    return _sigmoid(A @ model["w"])


def _logloss(y, p):
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _validated_model(db):
    rows = _training_rows(db)
    n = len(rows)
    meta = {
        "samples": n,
        "positives": int(sum(y for _, _, y in rows)),
        "negatives": int(n - sum(y for _, _, y in rows)),
        "accepted": False,
        "validation_logloss": None,
        "baseline_logloss": None,
        "mode": "COLD_START",
    }
    if n < 60 or meta["positives"] < 15 or meta["negatives"] < 15:
        return None, meta

    X = np.vstack([x for _, x, _ in rows])
    y = np.asarray([y for _, _, y in rows], dtype=float)
    split = max(40, int(n * 0.75))
    if n - split < 15:
        split = n - 15
    Xtr, Xv = X[:split], X[split:]
    ytr, yv = y[:split], y[split:]
    if len(set(ytr.tolist())) < 2 or len(set(yv.tolist())) < 2:
        return None, meta

    gate_model = _fit_logistic(Xtr, ytr)
    pv = _predict(gate_model, Xv)
    model_ll = _logloss(yv, pv)
    base_p = float(np.clip(ytr.mean(), 1e-3, 1 - 1e-3))
    base_ll = _logloss(yv, np.full(len(yv), base_p))
    meta["validation_logloss"] = model_ll
    meta["baseline_logloss"] = base_ll
    # The learner must beat the time-split prevalence baseline by at least 2%.
    if not (model_ll <= base_ll * 0.98):
        return None, meta

    meta["accepted"] = True
    meta["mode"] = "LEARNED_VALIDATED"
    return _fit_logistic(X, y), meta


def _realtime_context(market: str, symbol: str):
    out = {
        "quote_fresh": False,
        "spread_bps": None,
        "tca_samples": 0,
        "tca_avg_60s_bps": None,
        "tca_positive_60s_rate": None,
        "tca_execution_score": 50.0,
    }
    try:
        rt = RealtimeDB()
        quote = next((q for q in rt.quotes() if q.get("market") == market and str(q.get("symbol") or "").upper() == symbol.upper()), None)
        if quote:
            qt = _ts(quote.get("ts"))
            now = pd.Timestamp.now(tz="UTC")
            if qt is not None and 0 <= (now - qt).total_seconds() <= 15:
                out["quote_fresh"] = True
                spread = quote.get("spread_bps")
                out["spread_bps"] = _f(spread) if spread is not None else None

        with sqlite3.connect(rt.path, timeout=10) as c:
            c.row_factory = sqlite3.Row
            exists = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tca_events'").fetchone()
            if not exists:
                return out
            rows = [dict(r) for r in c.execute(
                "SELECT market,symbol,execution_cost_bps,markout_60s_bps FROM tca_events WHERE markout_60s_bps IS NOT NULL ORDER BY event_ts DESC LIMIT 500"
            )]
        symbol_rows = [r for r in rows if r.get("market") == market and str(r.get("symbol") or "").upper() == symbol.upper()]
        market_rows = [r for r in rows if r.get("market") == market]
        use = symbol_rows if len(symbol_rows) >= 5 else market_rows if len(market_rows) >= 10 else []
        if use:
            m = [_f(r.get("markout_60s_bps")) for r in use]
            costs = [_f(r.get("execution_cost_bps")) for r in use if r.get("execution_cost_bps") is not None]
            avg_m = float(np.mean(m))
            pos = float(np.mean([x > 0 for x in m]))
            avg_cost = float(np.mean(costs)) if costs else 0.0
            score = 50.0
            score += float(np.clip(avg_m / 25.0, -1, 1) * 20.0)
            score += (pos - 0.5) * 24.0
            score -= float(np.clip(avg_cost / 20.0, 0, 1) * 10.0)
            out.update({
                "tca_samples": len(use),
                "tca_avg_60s_bps": avg_m,
                "tca_positive_60s_rate": pos,
                "tca_execution_score": float(np.clip(score, 0, 100)),
            })
    except Exception:
        pass
    return out


def meta_entry_assessment(db, market: str, symbol: str, horizon: str, decision: dict) -> dict:
    """Second-layer entry quality assessment for virtual sizing only.

    Cold-start uses transparent alpha-quality features. A logistic learner can only
    activate after >=60 matched closed trades, balanced outcomes, and successful
    chronological holdout validation versus a prevalence baseline. It never
    increases size above the original model request in this stage.
    """
    heuristic = _heuristic_probability(decision)
    model, validation = _validated_model(db)
    if model is not None:
        learned = float(_predict(model, _features(decision)))
        # Shrink the learned estimate toward the transparent prior. Even validated
        # small-sample models do not receive full control immediately.
        blend = min(0.80, 0.55 + max(0, validation["samples"] - 60) / 400.0)
        alpha_prob = blend * learned + (1.0 - blend) * heuristic
    else:
        learned = None
        alpha_prob = heuristic

    rt = _realtime_context(market, symbol)
    if rt["tca_samples"] >= 10:
        meta_score = 0.82 * (alpha_prob * 100.0) + 0.18 * rt["tca_execution_score"]
    else:
        meta_score = alpha_prob * 100.0

    spread_penalty = 0.0
    spread = rt.get("spread_bps")
    if rt.get("quote_fresh") and spread is not None and spread > 20:
        spread_penalty = min(20.0, max(0.0, (spread - 20.0) * 0.35))
        meta_score -= spread_penalty
    meta_score = float(np.clip(meta_score, 0.0, 100.0))

    if meta_score >= 72:
        verdict, mult = "STRONG", 1.00
    elif meta_score >= 60:
        verdict, mult = "ALLOW", 1.00
    elif meta_score >= 50:
        verdict, mult = "CAUTION", 0.85
    else:
        verdict, mult = "SHADOW_ONLY", 0.60

    return {
        "meta_score": meta_score,
        "meta_probability": alpha_prob,
        "meta_verdict": verdict,
        "meta_multiplier": mult,
        "meta_mode": validation.get("mode", "COLD_START"),
        "meta_samples": int(validation.get("samples", 0) or 0),
        "meta_positives": int(validation.get("positives", 0) or 0),
        "meta_negatives": int(validation.get("negatives", 0) or 0),
        "meta_validation_logloss": validation.get("validation_logloss"),
        "meta_baseline_logloss": validation.get("baseline_logloss"),
        "meta_learned_probability": learned,
        "meta_heuristic_probability": heuristic,
        "meta_tca_samples": int(rt.get("tca_samples", 0) or 0),
        "meta_tca_execution_score": float(rt.get("tca_execution_score", 50.0)),
        "meta_tca_avg_60s_bps": rt.get("tca_avg_60s_bps"),
        "meta_tca_positive_60s_rate": rt.get("tca_positive_60s_rate"),
        "meta_quote_fresh": bool(rt.get("quote_fresh")),
        "meta_spread_bps": rt.get("spread_bps"),
        "meta_spread_penalty": spread_penalty,
        "meta_feature_names": FEATURE_NAMES,
        "meta_active_learned_model": bool(model is not None),
    }
