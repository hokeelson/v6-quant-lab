from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone

import numpy as np


METHOD = "HEURISTIC_FORWARD_SHADOW_V1"
EVALUATION_BARS = {"short": 6, "medium": 8, "long": 10}
MIN_TRADE_EV = {"short": 0.0030, "medium": 0.0040, "long": 0.0060}
CONFLICT_MARGIN = {"short": 0.0015, "medium": 0.0020, "long": 0.0030}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: float, low: float, high: float) -> float:
    return float(np.clip(float(value), low, high))


def _finite(value, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _utc_ns(value) -> int:
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.value)


def ensure_bidirectional_schema(db) -> None:
    """Research-only tables. They never participate in V2 accounting or routing."""
    with db._c() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS bidirectional_decisions(
              shadow_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              horizon TEXT NOT NULL,
              decision_bar TEXT NOT NULL,
              regime TEXT NOT NULL,
              selected_action TEXT NOT NULL,
              long_score REAL NOT NULL,
              short_score REAL NOT NULL,
              long_ev_proxy REAL NOT NULL,
              short_ev_proxy REAL NOT NULL,
              no_trade_ev REAL NOT NULL DEFAULT 0,
              min_trade_ev REAL NOT NULL,
              conflict_margin REAL NOT NULL,
              stop_distance REAL NOT NULL,
              target_distance REAL NOT NULL,
              evaluation_bars INTEGER NOT NULL,
              reason TEXT NOT NULL,
              method TEXT NOT NULL,
              ev_is_calibrated INTEGER NOT NULL DEFAULT 0,
              features_json TEXT NOT NULL,
              context_json TEXT NOT NULL,
              status TEXT NOT NULL,
              entry_bar TEXT,
              entry_price REAL,
              bars_held INTEGER NOT NULL DEFAULT 0,
              long_mfe_pct REAL NOT NULL DEFAULT 0,
              long_mae_pct REAL NOT NULL DEFAULT 0,
              short_mfe_pct REAL NOT NULL DEFAULT 0,
              short_mae_pct REAL NOT NULL DEFAULT 0,
              exit_bar TEXT,
              exit_price REAL,
              long_return_pct REAL,
              short_return_pct REAL,
              selected_return_pct REAL,
              best_realized_action TEXT,
              decision_correct INTEGER,
              created_at TEXT NOT NULL,
              UNIQUE(symbol,horizon,decision_bar)
            );
            CREATE INDEX IF NOT EXISTS idx_bidirectional_active
              ON bidirectional_decisions(symbol,horizon,status,decision_bar);
            """
        )


def score_bidirectional_decision(regime: dict, features: dict, horizon: str, context: dict | None,
                                 fee_rate: float) -> dict:
    """Score LONG/SHORT/NO_TRADE symmetrically for research only.

    The EV values are deliberately labelled proxies: the scores are not calibrated
    probabilities yet. Forward outcomes collected by this module are what will
    eventually allow calibration without contaminating the live V2 experiment.
    """
    state = str((regime or {}).get("state") or "INSUFFICIENT_DATA")
    ctx = dict(context or {})
    if not (features or {}).get("ready"):
        return {
            "selected_action": "NO_TRADE",
            "long_score": 0.0,
            "short_score": 0.0,
            "long_ev_proxy": -1.0,
            "short_ev_proxy": -1.0,
            "no_trade_ev": 0.0,
            "min_trade_ev": MIN_TRADE_EV.get(horizon, 0.003),
            "conflict_margin": CONFLICT_MARGIN.get(horizon, 0.0015),
            "stop_distance": 0.0,
            "target_distance": 0.0,
            "evaluation_bars": EVALUATION_BARS.get(horizon, 6),
            "reason": "Insufficient symbol history",
            "method": METHOD,
            "ev_is_calibrated": False,
        }

    atr = _clip(_finite(features.get("atr_pct"), 0.02), 0.003, 0.20)
    ret_fast = _finite(features.get("ret_fast"))
    ret_slow = _finite(features.get("ret_slow"))
    rs = _finite(features.get("relative_strength"))
    z = _finite(features.get("zscore20"))
    volume_z = _finite(features.get("volume_z"))
    breadth = features_breadth = ctx.get("breadth_above_ema20")
    btc24 = ctx.get("btc_return_24h")
    corr = ctx.get("avg_pairwise_correlation")

    long_score = 0.50
    short_score = 0.50

    regime_bias = {
        "TREND_UP": 0.16,
        "TREND_DOWN": -0.16,
        "SIDEWAYS": 0.0,
        "LOW_VOL_SIDEWAYS": 0.0,
        "HIGH_VOL_SIDEWAYS": 0.0,
        "PANIC": -0.04,
        "INSUFFICIENT_DATA": 0.0,
    }.get(state, 0.0)
    long_score += regime_bias
    short_score -= regime_bias

    fast_bias = _clip(ret_fast * 4.0, -0.10, 0.10)
    slow_bias = _clip(ret_slow * 2.0, -0.08, 0.08)
    rs_bias = _clip(rs * 3.0, -0.08, 0.08)
    long_score += fast_bias + slow_bias + rs_bias
    short_score -= fast_bias + slow_bias + rs_bias

    # Mean-reversion evidence matters mainly in non-trending states.
    if state in {"SIDEWAYS", "LOW_VOL_SIDEWAYS"}:
        if z <= -1.0:
            long_score += _clip((abs(z) - 1.0) * 0.08, 0.0, 0.16)
        elif z >= 1.0:
            short_score += _clip((abs(z) - 1.0) * 0.08, 0.0, 0.16)
        # Extreme volume makes a fade less trustworthy in either direction.
        if volume_z >= 2.5:
            long_score -= 0.06
            short_score -= 0.06

    if features_breadth is not None:
        breadth_bias = _clip((_finite(breadth, 0.5) - 0.5) * 0.16, -0.08, 0.08)
        long_score += breadth_bias
        short_score -= breadth_bias
    if btc24 is not None:
        btc_bias = _clip(_finite(btc24) * 2.0, -0.08, 0.08)
        long_score += btc_bias
        short_score -= btc_bias

    # High cross-asset correlation reduces directional confidence because a
    # single market shock can dominate symbol-specific evidence.
    if corr is not None and _finite(corr) >= 0.85:
        long_score = 0.5 + (long_score - 0.5) * 0.85
        short_score = 0.5 + (short_score - 0.5) * 0.85

    long_score = _clip(long_score, 0.05, 0.95)
    short_score = _clip(short_score, 0.05, 0.95)

    stop_distance = _clip(1.8 * atr, 0.015, 0.10)
    target_distance = _clip(3.0 * atr, 0.025, 0.18)
    round_trip_cost = max(0.0, 2.0 * float(fee_rate))
    long_ev = long_score * target_distance - (1.0 - long_score) * stop_distance - round_trip_cost
    short_ev = short_score * target_distance - (1.0 - short_score) * stop_distance - round_trip_cost
    min_ev = float(MIN_TRADE_EV.get(horizon, 0.003))
    margin = float(CONFLICT_MARGIN.get(horizon, 0.0015))

    risk_off = state in {"INSUFFICIENT_DATA", "PANIC", "HIGH_VOL_SIDEWAYS"}
    if risk_off:
        selected = "NO_TRADE"
        reason = f"Risk-off/unstable regime: {state}"
    elif max(long_ev, short_ev) < min_ev:
        selected = "NO_TRADE"
        reason = f"Neither direction clears minimum EV proxy {min_ev:.4f}"
    elif long_ev > 0 and short_ev > 0 and abs(long_ev - short_ev) < margin:
        selected = "NO_TRADE"
        reason = "LONG/SHORT conflict: positive edges too close to separate reliably"
    elif long_ev > short_ev:
        selected = "LONG"
        reason = "LONG has the stronger cost-adjusted EV proxy"
    else:
        selected = "SHORT"
        reason = "SHORT has the stronger cost-adjusted EV proxy"

    return {
        "selected_action": selected,
        "long_score": long_score,
        "short_score": short_score,
        "long_ev_proxy": float(long_ev),
        "short_ev_proxy": float(short_ev),
        "no_trade_ev": 0.0,
        "min_trade_ev": min_ev,
        "conflict_margin": margin,
        "stop_distance": stop_distance,
        "target_distance": target_distance,
        "evaluation_bars": int(EVALUATION_BARS.get(horizon, 6)),
        "reason": reason,
        "method": METHOD,
        "ev_is_calibrated": False,
    }


def record_bidirectional_decision(db, symbol: str, horizon: str, decision_bar: str, regime: dict,
                                  features: dict, context: dict | None, fee_rate: float) -> dict:
    ensure_bidirectional_schema(db)
    scored = score_bidirectional_decision(regime, features, horizon, context, fee_rate)
    with db._c() as c:
        c.execute(
            """INSERT OR IGNORE INTO bidirectional_decisions(
                 shadow_id,symbol,horizon,decision_bar,regime,selected_action,long_score,short_score,
                 long_ev_proxy,short_ev_proxy,no_trade_ev,min_trade_ev,conflict_margin,stop_distance,
                 target_distance,evaluation_bars,reason,method,ev_is_calibrated,features_json,context_json,
                 status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING_ENTRY',?)""",
            (
                uuid.uuid4().hex, symbol.upper(), horizon, decision_bar,
                str((regime or {}).get("state") or "UNKNOWN"), scored["selected_action"],
                float(scored["long_score"]), float(scored["short_score"]),
                float(scored["long_ev_proxy"]), float(scored["short_ev_proxy"]), 0.0,
                float(scored["min_trade_ev"]), float(scored["conflict_margin"]),
                float(scored["stop_distance"]), float(scored["target_distance"]),
                int(scored["evaluation_bars"]), str(scored["reason"]), METHOD, 0,
                json.dumps(features or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(context or {}, ensure_ascii=False, sort_keys=True), now_iso(),
            ),
        )
    return scored


def advance_bidirectional_evaluations(db, symbol: str, horizon: str, bar_time: str, row,
                                      fee_rate: float) -> None:
    """Advance all forward evaluations for one symbol/horizon on one closed bar."""
    ensure_bidirectional_schema(db)
    with db._c() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM bidirectional_decisions
               WHERE symbol=? AND horizon=? AND status IN ('PENDING_ENTRY','OPEN')
               ORDER BY decision_bar""",
            (symbol.upper(), horizon),
        ).fetchall()]

        for d in rows:
            if d["status"] == "PENDING_ENTRY":
                if _utc_ns(d["decision_bar"]) >= _utc_ns(bar_time):
                    continue
                entry = float(row.open)
                if entry <= 0:
                    continue
                c.execute(
                    """UPDATE bidirectional_decisions
                       SET status='OPEN',entry_bar=?,entry_price=? WHERE shadow_id=?""",
                    (bar_time, entry, d["shadow_id"]),
                )
                d["status"] = "OPEN"
                d["entry_bar"] = bar_time
                d["entry_price"] = entry

            entry = float(d.get("entry_price") or 0.0)
            if entry <= 0:
                continue
            high = float(row.high)
            low = float(row.low)
            close = float(row.close)
            long_mfe = max(float(d.get("long_mfe_pct") or 0.0), high / entry - 1.0)
            long_mae = min(float(d.get("long_mae_pct") or 0.0), low / entry - 1.0)
            short_mfe = max(float(d.get("short_mfe_pct") or 0.0), entry / max(low, 1e-12) - 1.0)
            short_mae = min(float(d.get("short_mae_pct") or 0.0), entry / max(high, 1e-12) - 1.0)
            bars_held = int(d.get("bars_held") or 0) + 1

            if bars_held < int(d["evaluation_bars"]):
                c.execute(
                    """UPDATE bidirectional_decisions SET bars_held=?,long_mfe_pct=?,long_mae_pct=?,
                       short_mfe_pct=?,short_mae_pct=? WHERE shadow_id=?""",
                    (bars_held, long_mfe, long_mae, short_mfe, short_mae, d["shadow_id"]),
                )
                continue

            fee = max(0.0, float(fee_rate))
            long_ret = (close * (1.0 - fee)) / (entry * (1.0 + fee)) - 1.0
            short_ret = (entry * (1.0 - fee) - close * (1.0 + fee)) / (entry * (1.0 + fee))
            realized_floor = float(d.get("min_trade_ev") or 0.0)
            if max(long_ret, short_ret) < realized_floor:
                best = "NO_TRADE"
            elif long_ret >= short_ret:
                best = "LONG"
            else:
                best = "SHORT"
            selected = str(d.get("selected_action") or "NO_TRADE")
            selected_ret = long_ret if selected == "LONG" else short_ret if selected == "SHORT" else 0.0
            c.execute(
                """UPDATE bidirectional_decisions
                   SET status='CLOSED',bars_held=?,long_mfe_pct=?,long_mae_pct=?,short_mfe_pct=?,short_mae_pct=?,
                       exit_bar=?,exit_price=?,long_return_pct=?,short_return_pct=?,selected_return_pct=?,
                       best_realized_action=?,decision_correct=? WHERE shadow_id=?""",
                (
                    bars_held, long_mfe, long_mae, short_mfe, short_mae, bar_time, close,
                    float(long_ret), float(short_ret), float(selected_ret), best,
                    int(selected == best), d["shadow_id"],
                ),
            )


def bidirectional_summary(db) -> dict:
    ensure_bidirectional_schema(db)
    with db._c() as c:
        total = int(c.execute("SELECT COUNT(*) FROM bidirectional_decisions").fetchone()[0])
        active = int(c.execute(
            "SELECT COUNT(*) FROM bidirectional_decisions WHERE status IN ('PENDING_ENTRY','OPEN')"
        ).fetchone()[0])
        closed = [dict(r) for r in c.execute(
            "SELECT * FROM bidirectional_decisions WHERE status='CLOSED' ORDER BY exit_bar DESC LIMIT 5000"
        ).fetchall()]

    by_action: dict[str, dict] = {}
    correct = 0
    no_trade_avoided_losses = 0
    no_trade_missed_opportunities = 0
    selected_return_sum = 0.0
    for r in closed:
        action = str(r.get("selected_action") or "NO_TRADE")
        ok = bool(r.get("decision_correct"))
        correct += int(ok)
        selected_ret = _finite(r.get("selected_return_pct"))
        selected_return_sum += selected_ret
        b = by_action.setdefault(action, {"closed": 0, "correct": 0, "selected_return_sum": 0.0})
        b["closed"] += 1
        b["correct"] += int(ok)
        b["selected_return_sum"] += selected_ret
        if action == "NO_TRADE":
            long_ret = _finite(r.get("long_return_pct"))
            short_ret = _finite(r.get("short_return_pct"))
            if long_ret <= 0 and short_ret <= 0:
                no_trade_avoided_losses += 1
            if str(r.get("best_realized_action") or "") in {"LONG", "SHORT"}:
                no_trade_missed_opportunities += 1

    for b in by_action.values():
        n = int(b["closed"])
        b["accuracy"] = b["correct"] / n if n else None
        b["avg_selected_return_pct"] = b.pop("selected_return_sum") / n if n else None

    n_closed = len(closed)
    return {
        "status": "ACTIVE",
        "method": METHOD,
        "forward_only": True,
        "observation_only": True,
        "changes_v2_execution": False,
        "ev_is_calibrated": False,
        "ev_note": "EV proxy uses heuristic directional scores; forward outcomes are collected for later calibration.",
        "total_decisions": total,
        "active_evaluations": active,
        "closed_evaluations": n_closed,
        "decision_accuracy": correct / n_closed if n_closed else None,
        "avg_selected_return_pct": selected_return_sum / n_closed if n_closed else None,
        "no_trade_avoided_losses": no_trade_avoided_losses,
        "no_trade_missed_opportunities": no_trade_missed_opportunities,
        "by_selected_action": by_action,
    }


def recent_bidirectional_decisions(db, limit: int = 100) -> list[dict]:
    ensure_bidirectional_schema(db)
    with db._c() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM bidirectional_decisions ORDER BY decision_bar DESC LIMIT ?", (int(limit),)
        ).fetchall()]
