from __future__ import annotations

import json
import math
from collections import defaultdict


def _f(value, default=None):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _market_from_account(account_id: str) -> str:
    aid = str(account_id or "")
    if aid.startswith("twstock_"):
        return "twstock"
    if aid.startswith("crypto_"):
        return "crypto"
    if aid.startswith("stock_"):
        return "stock"
    return ""


def _compound_return(rows):
    wealth = 1.0
    for row in rows:
        r = _f(row.get("return_pct"), 0.0) or 0.0
        wealth *= max(1e-9, 1.0 + r)
    return wealth - 1.0


def _profit_factor(rows):
    gains = sum(max(0.0, _f(r.get("realized_pnl"), 0.0) or 0.0) for r in rows)
    losses = abs(sum(min(0.0, _f(r.get("realized_pnl"), 0.0) or 0.0) for r in rows))
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return gains / losses


def _max_loss_streak(rows):
    streak = best = 0
    for row in sorted(rows, key=lambda x: str(x.get("exit_bar") or "")):
        pnl = _f(row.get("realized_pnl"), 0.0) or 0.0
        if pnl < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def _per_trade(total_return, trades):
    total_return = _f(total_return)
    trades = int(trades or 0)
    if total_return is None or trades <= 0 or total_return <= -1.0:
        return None
    try:
        return (1.0 + total_return) ** (1.0 / trades) - 1.0
    except Exception:
        return None


def _finite_pf(value):
    x = _f(value)
    return x if x is not None and x < 1e9 else None


def expected_live_deviation_snapshot(db, trade_limit: int = 5000) -> dict:
    """Compare calibration OOS expectations with forward virtual realized trades.

    Diagnostic only: this layer does not alter strategy decisions or position size.
    Small samples remain LEARNING so a few lucky/unlucky trades cannot kill a model.
    """
    trades = db.recent_trades(trade_limit)
    grouped = defaultdict(list)
    for t in trades:
        key = (
            _market_from_account(t.get("account_id")),
            str(t.get("symbol") or "").upper(),
            str(t.get("horizon") or ""),
            str(t.get("strategy") or ""),
        )
        grouped[key].append(t)

    rows = []
    for m in db.models():
        market = str(m.get("market") or "")
        symbol = str(m.get("symbol") or "").upper()
        horizon = str(m.get("horizon") or "")
        strategy = str(m.get("strategy") or "")
        live = grouped.get((market, symbol, horizon, strategy), [])

        try:
            diagnostics = json.loads(m.get("diagnostics_json") or "{}")
        except Exception:
            diagnostics = {}
        oos = diagnostics.get("oos_metrics") or {}

        n = len(live)
        wins = sum(1 for x in live if (_f(x.get("realized_pnl"), 0.0) or 0.0) > 0)
        losses = sum(1 for x in live if (_f(x.get("realized_pnl"), 0.0) or 0.0) < 0)
        live_win = wins / n if n else None
        live_total = _compound_return(live) if n else None
        live_pf_raw = _profit_factor(live) if n else None
        live_pf = _finite_pf(live_pf_raw)
        live_pf_infinite = bool(live_pf_raw is not None and not math.isfinite(float(live_pf_raw)))
        loss_streak = _max_loss_streak(live) if n else 0

        oos_n = int(oos.get("closed_trades", 0) or 0)
        oos_total = _f(oos.get("total_return"))
        oos_win = _f(oos.get("win_rate"))
        oos_pf_raw = oos.get("profit_factor")
        oos_pf = _finite_pf(oos_pf_raw)
        try:
            oos_pf_infinite = bool(oos_pf_raw is not None and not math.isfinite(float(oos_pf_raw)))
        except Exception:
            oos_pf_infinite = False
        oos_sharpe = _f(oos.get("sharpe"))
        oos_dd = _f(oos.get("max_drawdown"))
        oos_per_trade = _per_trade(oos_total, oos_n)
        live_per_trade = _per_trade(live_total, n)

        win_gap = (oos_win - live_win) if oos_win is not None and live_win is not None else None
        pf_gap = (oos_pf - live_pf) if oos_pf is not None and live_pf is not None else None
        per_trade_gap = (
            oos_per_trade - live_per_trade
            if oos_per_trade is not None and live_per_trade is not None
            else None
        )

        raw = 0.0
        reasons = []
        if oos_total is not None and oos_total > 0 and live_total is not None and live_total < 0:
            raw += 35.0
            reasons.append("OOS_POSITIVE_LIVE_NEGATIVE")
        if win_gap is not None and win_gap > 0.15:
            raw += min(25.0, 25.0 * win_gap / 0.30)
            reasons.append("WIN_RATE_DETERIORATION")
        if oos_pf is not None and oos_pf > 1.0 and live_pf is not None and live_pf < 1.0:
            raw += min(25.0, 25.0 * max(0.0, oos_pf - live_pf) / max(oos_pf, 1.0))
            reasons.append("PROFIT_FACTOR_DETERIORATION")
        if oos_per_trade is not None and oos_per_trade > 0 and live_per_trade is not None and live_per_trade < 0:
            raw += 20.0
            reasons.append("EXPECTANCY_SIGN_REVERSAL")
        if loss_streak >= 4:
            raw += min(15.0, 5.0 + 2.5 * (loss_streak - 4))
            reasons.append("LOSS_STREAK")

        evidence = min(1.0, n / 20.0)
        score = min(100.0, raw * (0.35 + 0.65 * evidence))
        if n < 5:
            state = "LEARNING"
            multiplier = 1.00
        elif score >= 65:
            state = "SEVERE_DIVERGENCE"
            multiplier = 0.40
        elif score >= 40:
            state = "DIVERGING"
            multiplier = 0.65
        elif score >= 20:
            state = "WATCH"
            multiplier = 0.85
        else:
            state = "NORMAL"
            multiplier = 1.00

        rows.append({
            "market": market,
            "symbol": symbol,
            "horizon": horizon,
            "strategy": strategy,
            "performance_key": f"{market}:{symbol}:{horizon}:{strategy}",
            "live_closed_trades": n,
            "live_wins": wins,
            "live_losses": losses,
            "live_win_rate": live_win,
            "live_compound_return": live_total,
            "live_profit_factor": live_pf,
            "live_profit_factor_infinite": live_pf_infinite,
            "live_per_trade_return": live_per_trade,
            "live_max_loss_streak": loss_streak,
            "oos_closed_trades": oos_n,
            "oos_win_rate": oos_win,
            "oos_total_return": oos_total,
            "oos_profit_factor": oos_pf,
            "oos_profit_factor_infinite": oos_pf_infinite,
            "oos_sharpe": oos_sharpe,
            "oos_max_drawdown": oos_dd,
            "oos_per_trade_return": oos_per_trade,
            "win_rate_gap": win_gap,
            "profit_factor_gap": pf_gap,
            "per_trade_return_gap": per_trade_gap,
            "evidence_weight": evidence,
            "deviation_score": score,
            "state": state,
            "suggested_confidence_multiplier": multiplier,
            "reasons": reasons,
            "shadow_only": True,
        })

    rows.sort(key=lambda x: (x["deviation_score"], x["live_closed_trades"]), reverse=True)
    return {
        "status": "AVAILABLE",
        "shadow_only": True,
        "active_sizing": False,
        "rows": rows,
        "summary": {
            "models": len(rows),
            "with_live_trades": sum(1 for r in rows if r["live_closed_trades"] > 0),
            "learning": sum(1 for r in rows if r["state"] == "LEARNING"),
            "watch": sum(1 for r in rows if r["state"] == "WATCH"),
            "diverging": sum(1 for r in rows if r["state"] == "DIVERGING"),
            "severe": sum(1 for r in rows if r["state"] == "SEVERE_DIVERGENCE"),
            "broker_order_api_calls": 0,
        },
    }
