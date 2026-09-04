from __future__ import annotations

import json
import math


def _profit_factor(rows: list[dict]) -> float | None:
    wins = sum(max(0.0, float(r.get("realized_pnl") or 0.0)) for r in rows)
    losses = sum(abs(min(0.0, float(r.get("realized_pnl") or 0.0))) for r in rows)
    if losses <= 0:
        return None if wins <= 0 else math.inf
    return wins / losses


def _metrics(rows: list[dict]) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if float(r.get("realized_pnl") or 0.0) > 0)
    pnl = sum(float(r.get("realized_pnl") or 0.0) for r in rows)
    avg_ret = (
        sum(float(r.get("return_pct") or 0.0) for r in rows) / n if n else 0.0
    )
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n if n else 0.0,
        "realized_pnl": pnl,
        "avg_return": avg_ret,
        "profit_factor": _profit_factor(rows),
    }


def build_performance_summary(db, account_id: str = "crypto") -> dict:
    trades = [
        r for r in db.recent_trades(5000)
        if str(r.get("account_id") or "") == account_id
    ]
    equity = db.equity(account_id, 10000)
    max_drawdown = min(
        [float(r.get("drawdown") or 0.0) for r in equity] or [0.0]
    )

    by_horizon = {}
    for hz in ("short", "medium", "long"):
        by_horizon[hz] = _metrics([
            r for r in trades if str(r.get("horizon") or "") == hz
        ])

    by_exit = {}
    for reason in sorted({str(r.get("exit_reason") or "UNKNOWN") for r in trades}):
        by_exit[reason] = _metrics([
            r for r in trades if str(r.get("exit_reason") or "UNKNOWN") == reason
        ])

    sizing_by_order = {}
    for row in db.diagnostics(10000):
        if str(row.get("category") or "") != "RISK_SIZING":
            continue
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            continue
        oid = payload.get("order_id")
        if oid:
            sizing_by_order[str(oid)] = payload

    context_reduced = []
    context_normal = []
    context_unknown = []
    for trade in trades:
        payload = sizing_by_order.get(str(trade.get("entry_order_id") or ""))
        if not payload:
            context_unknown.append(trade)
            continue
        mult = payload.get("binance_context_multiplier")
        if mult is None:
            context_unknown.append(trade)
        elif float(mult) < 0.999999:
            context_reduced.append(trade)
        else:
            context_normal.append(trade)

    return {
        "overall": _metrics(trades),
        "max_drawdown": max_drawdown,
        "by_horizon": by_horizon,
        "by_exit_reason": by_exit,
        "binance_context_comparison": {
            "reduced": _metrics(context_reduced),
            "normal": _metrics(context_normal),
            "unknown": _metrics(context_unknown),
        },
    }
