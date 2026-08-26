from __future__ import annotations


RISK_LIMITS = {
    "short": {
        "max_positions": 6,
        "max_gross_pct": 0.35,
        "max_strategy_pct": 0.25,
        "max_regime_pct": 0.30,
        "min_entry_pct": 0.005,
    },
    "medium": {
        "max_positions": 5,
        "max_gross_pct": 0.32,
        "max_strategy_pct": 0.22,
        "max_regime_pct": 0.27,
        "min_entry_pct": 0.005,
    },
    "long": {
        "max_positions": 4,
        "max_gross_pct": 0.30,
        "max_strategy_pct": 0.20,
        "max_regime_pct": 0.25,
        "min_entry_pct": 0.005,
    },
}


def _limits(horizon: str) -> dict:
    return dict(RISK_LIMITS.get(str(horizon), RISK_LIMITS["short"]))


def _records(state: dict) -> list[dict]:
    return list(state.get("positions") or []) + list(state.get("pending_orders") or [])


def portfolio_status(initial_equity: float, state: dict, horizon: str) -> dict:
    initial = max(float(initial_equity or 0.0), 0.0)
    limits = _limits(horizon)
    records = _records(state)
    gross = sum(max(float(r.get("notional") or 0.0), 0.0) for r in records)
    by_strategy: dict[str, float] = {}
    by_regime: dict[str, float] = {}
    for r in records:
        notional = max(float(r.get("notional") or 0.0), 0.0)
        strategy = str(r.get("strategy") or "UNKNOWN")
        regime = str(r.get("regime") or "UNKNOWN")
        by_strategy[strategy] = by_strategy.get(strategy, 0.0) + notional
        by_regime[regime] = by_regime.get(regime, 0.0) + notional

    gross_cap = initial * float(limits["max_gross_pct"])
    strategy_cap = initial * float(limits["max_strategy_pct"])
    regime_cap = initial * float(limits["max_regime_pct"])
    breaches = []
    if len(records) >= int(limits["max_positions"]):
        breaches.append("MAX_POSITIONS")
    if gross >= gross_cap - 1e-9:
        breaches.append("MAX_GROSS_EXPOSURE")
    if any(v >= strategy_cap - 1e-9 for v in by_strategy.values()):
        breaches.append("MAX_STRATEGY_EXPOSURE")
    if any(v >= regime_cap - 1e-9 for v in by_regime.values()):
        breaches.append("MAX_REGIME_EXPOSURE")

    return {
        "horizon": str(horizon),
        "status": "LIMITED" if breaches else "NORMAL",
        "breaches": breaches,
        "open_positions": len(state.get("positions") or []),
        "pending_orders": len(state.get("pending_orders") or []),
        "reserved_slots": len(records),
        "gross_notional": gross,
        "gross_pct": gross / initial if initial else None,
        "strategy_notional": by_strategy,
        "regime_notional": by_regime,
        "limits": limits,
        "caps": {
            "gross_notional": gross_cap,
            "strategy_notional": strategy_cap,
            "regime_notional": regime_cap,
        },
    }


def govern_entry(
    initial_equity: float,
    requested_notional: float,
    state: dict,
    horizon: str,
    strategy: str,
    regime: str,
) -> dict:
    initial = max(float(initial_equity or 0.0), 0.0)
    requested = max(float(requested_notional or 0.0), 0.0)
    status = portfolio_status(initial, state, horizon)
    limits = status["limits"]
    strategy = str(strategy or "UNKNOWN")
    regime = str(regime or "UNKNOWN")

    if requested <= 0 or initial <= 0:
        return {**status, "requested_notional": requested, "approved_notional": 0.0, "reason": "NO_CAPITAL"}
    if int(status["reserved_slots"]) >= int(limits["max_positions"]):
        return {**status, "requested_notional": requested, "approved_notional": 0.0, "reason": "MAX_POSITIONS"}

    gross_room = max(float(status["caps"]["gross_notional"]) - float(status["gross_notional"]), 0.0)
    strategy_used = float(status["strategy_notional"].get(strategy, 0.0))
    regime_used = float(status["regime_notional"].get(regime, 0.0))
    strategy_room = max(float(status["caps"]["strategy_notional"]) - strategy_used, 0.0)
    regime_room = max(float(status["caps"]["regime_notional"]) - regime_used, 0.0)
    approved = min(requested, gross_room, strategy_room, regime_room)

    minimum = initial * float(limits["min_entry_pct"])
    if approved < minimum:
        if gross_room < minimum:
            reason = "MAX_GROSS_EXPOSURE"
        elif strategy_room < minimum:
            reason = "MAX_STRATEGY_EXPOSURE"
        elif regime_room < minimum:
            reason = "MAX_REGIME_EXPOSURE"
        else:
            reason = "BELOW_MIN_ENTRY"
        approved = 0.0
    elif approved + 1e-9 < requested:
        reason = "DOWNSIZED_BY_PORTFOLIO_RISK"
    else:
        reason = "APPROVED"

    return {
        **status,
        "requested_notional": requested,
        "approved_notional": approved,
        "strategy": strategy,
        "regime": regime,
        "strategy_used": strategy_used,
        "regime_used": regime_used,
        "reason": reason,
    }
