from __future__ import annotations

import os

from .pretrade_risk import build_pretrade_risk_snapshot
from .pro_risk_engine import portfolio_risk_snapshot, strategy_health_snapshot


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _min_multiplier() -> float:
    try:
        return max(0.10, min(1.0, float(os.getenv("V6_MIN_ACTIVE_SIZE_MULTIPLIER", "0.25"))))
    except Exception:
        return 0.25


def _find_pretrade_row(snapshot: dict, market: str, symbol: str, horizon: str) -> dict | None:
    for row in snapshot.get("candidates") or []:
        if (
            str(row.get("market") or "") == market
            and str(row.get("symbol") or "").upper() == symbol.upper()
            and str(row.get("horizon") or "") == horizon
        ):
            return row
    return None


def _find_health_row(rows: list[dict], market: str, horizon: str, strategy: str, regime: str | None = None) -> dict | None:
    for row in rows:
        if str(row.get("market") or "") != market:
            continue
        if str(row.get("horizon") or "") != horizon:
            continue
        if str(row.get("strategy") or "") != str(strategy or ""):
            continue
        if regime is not None and str(row.get("regime") or "") != str(regime or ""):
            continue
        return row
    return None


def active_entry_sizing(db, cache, market: str, symbol: str, horizon: str, decision: dict,
                        requested_notional: float) -> dict:
    """Return the notional to use for a virtual BUY fill.

    This is intentionally a sizing layer, not a hard block. The original model
    decision remains unchanged in the decisions table; only the virtual fill size
    is adjusted. Broker order APIs are not used.
    """
    original = max(0.0, float(requested_notional or 0.0))
    result = {
        "original_notional": original,
        "adjusted_notional": original,
        "combined_multiplier": 1.0,
        "portfolio_multiplier": 1.0,
        "pretrade_multiplier": 1.0,
        "global_multiplier": 1.0,
        "strategy_multiplier": 1.0,
        "strategy_state": "LEARNING",
        "regime_multiplier": 1.0,
        "regime_state": "LEARNING",
        "pretrade_verdict": "ALLOW",
        "pretrade_score": 0.0,
        "flags": "無明顯組合衝突",
        "active_portfolio_sizing": _flag("V6_ACTIVE_PORTFOLIO_SIZING", True),
        "active_strategy_health_sizing": _flag("V6_ACTIVE_STRATEGY_HEALTH_SIZING", True),
        "error": None,
    }
    if original <= 0:
        return result

    try:
        if result["active_portfolio_sizing"]:
            pre = build_pretrade_risk_snapshot(db, cache)
            row = _find_pretrade_row(pre, market, symbol, horizon)
            if row:
                result["pretrade_multiplier"] = float(row.get("shadow_size_multiplier", 1.0) or 1.0)
                result["pretrade_verdict"] = str(row.get("verdict") or "ALLOW")
                result["pretrade_score"] = float(row.get("risk_score", 0.0) or 0.0)
                result["flags"] = str(row.get("flags") or "無明顯組合衝突")

            portfolio = portfolio_risk_snapshot(db, cache)
            global_row = next(
                (r for r in (portfolio.get("groups") or []) if str(r.get("group") or "") == "GLOBAL"),
                None,
            )
            if global_row:
                result["global_multiplier"] = float(global_row.get("shadow_risk_multiplier", 1.0) or 1.0)

            # Avoid double-penalizing the same exposure/correlation evidence.
            result["portfolio_multiplier"] = min(result["pretrade_multiplier"], result["global_multiplier"])

        if result["active_strategy_health_sizing"]:
            health = strategy_health_snapshot(db)
            strategy = str(decision.get("strategy") or "")
            regime = str(decision.get("regime") or "")
            srow = _find_health_row(health.get("strategies") or [], market, horizon, strategy)
            rrow = _find_health_row(health.get("regimes") or [], market, horizon, strategy, regime)
            if srow:
                result["strategy_multiplier"] = float(srow.get("shadow_weight_multiplier", 1.0) or 1.0)
                result["strategy_state"] = str(srow.get("state") or "LEARNING")
            if rrow:
                result["regime_multiplier"] = float(rrow.get("shadow_weight_multiplier", 1.0) or 1.0)
                result["regime_state"] = str(rrow.get("state") or "LEARNING")

            # Strategy and Strategy×Regime are overlapping evidence; use the more
            # conservative one rather than multiplying them together.
            health_multiplier = min(result["strategy_multiplier"], result["regime_multiplier"])
        else:
            health_multiplier = 1.0

        combined = result["portfolio_multiplier"] * health_multiplier
        combined = max(_min_multiplier(), min(1.0, float(combined)))
        result["combined_multiplier"] = combined
        result["adjusted_notional"] = original * combined
    except Exception as exc:
        # Fail open for research continuity: a diagnostics-layer error must not
        # silently delete an otherwise valid forward trade.
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["combined_multiplier"] = 1.0
        result["adjusted_notional"] = original

    return result
