from __future__ import annotations

import os

from .backtest import ExecutionCosts
from .data_quality_drift import assess_pair
from .decision_engine import HORIZON_SPECS
from .expected_live_sizing import expected_live_sizing_assessment
from .leverage_guard import cost_aware_leverage_room, projected_post_fill
from .meta_model import meta_entry_assessment
from .pretrade_risk import build_pretrade_risk_snapshot
from .pro_risk_engine import portfolio_risk_snapshot, strategy_health_snapshot
from .symbol_strategy_health import find_symbol_strategy_health, symbol_strategy_health_snapshot


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


def _account_marks(db, account_id: str) -> tuple[float, float, float]:
    acct = db.account(account_id)
    if not acct:
        return 0.0, 0.0, 0.0
    cash = float(acct.get("cash") or 0.0)
    marks = db.marks(account_id)
    gross = 0.0
    for pos in db.positions(account_id):
        px = float(marks.get(pos.get("symbol"), pos.get("avg_entry") or 0.0) or 0.0)
        gross += max(0.0, float(pos.get("qty") or 0.0) * px)
    return cash, gross, cash + gross


def _cost_rate(market: str) -> float:
    costs = ExecutionCosts(0, 3, 2) if market == "stock" else ExecutionCosts(10, 5, 4)
    return float(costs.one_way_rate)


def active_entry_sizing(db, cache, market: str, symbol: str, horizon: str, decision: dict,
                        requested_notional: float) -> dict:
    """Return the notional to use for a virtual BUY fill.

    Independent evidence layers are combined at the last possible moment:
    portfolio/pre-trade risk, strategy health, symbol×strategy health, OOS-vs-live
    generalization health, second-layer Meta Model, and data-quality/concept-drift
    health. A final cost-aware leverage hard guard can reduce the virtual fill
    further. The original model decision is preserved and no broker order API is used.
    """
    original = max(0.0, float(requested_notional or 0.0))
    result = {
        "original_notional": original,
        "adjusted_notional": original,
        "pre_execution_adjusted_notional": original,
        "combined_multiplier": 1.0,
        "portfolio_multiplier": 1.0,
        "pretrade_multiplier": 1.0,
        "global_multiplier": 1.0,
        "strategy_multiplier": 1.0,
        "strategy_state": "LEARNING",
        "regime_multiplier": 1.0,
        "regime_state": "LEARNING",
        "symbol_strategy_multiplier": 1.0,
        "symbol_strategy_state": "LEARNING",
        "symbol_strategy_samples": 0,
        "symbol_strategy_failure_votes": 0,
        "symbol_strategy_profit_factor": None,
        "symbol_strategy_weighted_win_rate": None,
        "symbol_strategy_weighted_avg_return": None,
        "symbol_strategy_performance_key": None,
        "expected_live_multiplier": 1.0,
        "expected_live_state": "LEARNING",
        "expected_live_samples": 0,
        "expected_live_deviation_score": None,
        "expected_live_reasons": [],
        "expected_live_performance_key": None,
        "expected_live_evidence_weight": 0.0,
        "meta_multiplier": 1.0,
        "meta_score": None,
        "meta_probability": None,
        "meta_verdict": "LEARNING",
        "meta_mode": "COLD_START",
        "meta_samples": 0,
        "meta_tca_samples": 0,
        "meta_spread_bps": None,
        "quality_drift_multiplier": 1.0,
        "data_multiplier": 1.0,
        "drift_multiplier": 1.0,
        "data_status": "UNKNOWN",
        "drift_status": "LEARNING",
        "quality_score": None,
        "drift_score": None,
        "quality_reasons": [],
        "drift_reasons": [],
        "pretrade_verdict": "ALLOW",
        "pretrade_score": 0.0,
        "flags": "無明顯組合衝突",
        "active_portfolio_sizing": _flag("V6_ACTIVE_PORTFOLIO_SIZING", True),
        "active_strategy_health_sizing": _flag("V6_ACTIVE_STRATEGY_HEALTH_SIZING", True),
        "active_symbol_strategy_health_sizing": _flag("V6_ACTIVE_SYMBOL_STRATEGY_HEALTH_SIZING", True),
        "active_expected_live_sizing": _flag("V6_ACTIVE_EXPECTED_LIVE_SIZING", True),
        "active_meta_sizing": _flag("V6_ACTIVE_META_SIZING", True),
        "active_data_quality_sizing": _flag("V6_ACTIVE_DATA_QUALITY_SIZING", True),
        "active_leverage_hard_guard": _flag("V6_ACTIVE_LEVERAGE_HARD_GUARD", True),
        "leverage_guard_applied": False,
        "leverage_guard_multiplier": 1.0,
        "legacy_leverage_room": None,
        "cost_adjusted_leverage_room": None,
        "max_leverage": None,
        "target_leverage_cap": None,
        "projected_post_fill_leverage": None,
        "projected_post_fill_equity": None,
        "projected_post_fill_gross": None,
        "leverage_guard_error": None,
        "meta_error": None,
        "quality_error": None,
        "symbol_strategy_error": None,
        "expected_live_error": None,
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

            # Same exposure/correlation evidence appears in both snapshots; use
            # the more conservative multiplier rather than multiplying twice.
            result["portfolio_multiplier"] = min(result["pretrade_multiplier"], result["global_multiplier"])

        health_multiplier = 1.0
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
            # Strategy and Strategy×Regime are overlapping evidence too.
            health_multiplier = min(result["strategy_multiplier"], result["regime_multiplier"])

        if result["active_symbol_strategy_health_sizing"]:
            try:
                strategy = str(decision.get("strategy") or "")
                symbol_health = symbol_strategy_health_snapshot(db)
                hrow = find_symbol_strategy_health(symbol_health, market, symbol, horizon, strategy)
                if hrow:
                    result["symbol_strategy_multiplier"] = float(hrow.get("shadow_weight_multiplier", 1.0) or 1.0)
                    result["symbol_strategy_state"] = str(hrow.get("state") or "LEARNING")
                    result["symbol_strategy_samples"] = int(hrow.get("samples", 0) or 0)
                    result["symbol_strategy_failure_votes"] = int(hrow.get("failure_votes", 0) or 0)
                    result["symbol_strategy_profit_factor"] = hrow.get("profit_factor")
                    result["symbol_strategy_weighted_win_rate"] = hrow.get("weighted_win_rate")
                    result["symbol_strategy_weighted_avg_return"] = hrow.get("weighted_avg_return")
                    result["symbol_strategy_performance_key"] = hrow.get("performance_key")
                # Symbol×strategy is a narrower view of the same realized trades.
                # Do not multiply it by broad strategy health; use the stricter view.
                health_multiplier = min(health_multiplier, result["symbol_strategy_multiplier"])
            except Exception as exc:
                result["symbol_strategy_error"] = f"{type(exc).__name__}: {exc}"

        expected_live_multiplier = 1.0
        if result["active_expected_live_sizing"]:
            try:
                strategy = str(decision.get("strategy") or "")
                deviation = expected_live_sizing_assessment(db, market, symbol, horizon, strategy)
                result["expected_live_multiplier"] = float(deviation.get("expected_live_multiplier", 1.0) or 1.0)
                result["expected_live_state"] = str(deviation.get("expected_live_state") or "LEARNING")
                result["expected_live_samples"] = int(deviation.get("expected_live_samples", 0) or 0)
                result["expected_live_deviation_score"] = deviation.get("expected_live_deviation_score")
                result["expected_live_reasons"] = list(deviation.get("expected_live_reasons") or [])
                result["expected_live_performance_key"] = deviation.get("expected_live_performance_key")
                result["expected_live_evidence_weight"] = float(deviation.get("expected_live_evidence_weight", 0.0) or 0.0)
                expected_live_multiplier = result["expected_live_multiplier"]
            except Exception as exc:
                # Generalization diagnostics are fail-open so a read/analytics
                # problem cannot erase an otherwise valid forward virtual trade.
                result["expected_live_error"] = f"{type(exc).__name__}: {exc}"
                expected_live_multiplier = 1.0

        meta_multiplier = 1.0
        if result["active_meta_sizing"]:
            try:
                meta = meta_entry_assessment(db, market, symbol, horizon, decision)
                result.update(meta)
                meta_multiplier = float(meta.get("meta_multiplier", 1.0) or 1.0)
            except Exception as exc:
                # Meta is intentionally fail-open. A second-layer analytics issue
                # must never delete a valid forward trade.
                result["meta_error"] = f"{type(exc).__name__}: {exc}"
                meta_multiplier = 1.0

        quality_multiplier = 1.0
        if result["active_data_quality_sizing"]:
            try:
                health = assess_pair(db, cache, market, symbol, horizon)
                result["quality_drift_multiplier"] = float(health.get("quality_drift_multiplier", 1.0) or 1.0)
                result["data_multiplier"] = float(health.get("data_multiplier", 1.0) or 1.0)
                result["drift_multiplier"] = float(health.get("drift_multiplier", 1.0) or 1.0)
                result["data_status"] = str(health.get("data_status") or "UNKNOWN")
                result["drift_status"] = str(health.get("drift_status") or "LEARNING")
                result["quality_score"] = health.get("quality_score")
                result["drift_score"] = health.get("drift_score")
                result["quality_reasons"] = [] if result["data_status"] == "OK" else [result["data_status"]]
                result["drift_reasons"] = health.get("reasons") or []
                result["quality_detail"] = health
                quality_multiplier = result["quality_drift_multiplier"]
            except Exception as exc:
                result["quality_error"] = f"{type(exc).__name__}: {exc}"
                quality_multiplier = 1.0

        # Expected-vs-live is an additional generalization test: absolute realized
        # health can be weak while the more important question is whether live
        # behavior materially contradicts the model's own OOS expectation. It only
        # activates after >=5 closed trades, so small-sample combinations remain 1x.
        combined = (
            result["portfolio_multiplier"]
            * health_multiplier
            * expected_live_multiplier
            * meta_multiplier
            * quality_multiplier
        )
        combined = max(_min_multiplier(), min(1.0, float(combined)))
        result["combined_multiplier"] = combined
        result["adjusted_notional"] = original * combined
        result["pre_execution_adjusted_notional"] = result["adjusted_notional"]

        # This final guard is execution safety, not another evidence score. It may
        # reduce below the normal minimum sizing floor when the account is near its
        # hard leverage cap. Taiwan is cash-only and keeps its existing cash clamp.
        if result["active_leverage_hard_guard"] and market in ("stock", "crypto"):
            try:
                account_id = f"{market}_{horizon}"
                _, gross, equity = _account_marks(db, account_id)
                max_leverage = float(HORIZON_SPECS[horizon]["max_leverage"])
                rate = _cost_rate(market)
                guard = cost_aware_leverage_room(equity, gross, max_leverage, rate, 0.005)
                result["legacy_leverage_room"] = guard.get("legacy_room")
                result["cost_adjusted_leverage_room"] = guard.get("cost_adjusted_room")
                result["max_leverage"] = guard.get("max_leverage")
                result["target_leverage_cap"] = guard.get("target_leverage_cap")
                guarded = min(float(result["adjusted_notional"]), float(guard.get("cost_adjusted_room") or 0.0))
                result["leverage_guard_applied"] = guarded + 1e-9 < float(result["adjusted_notional"])
                result["leverage_guard_multiplier"] = (
                    guarded / float(result["adjusted_notional"])
                    if float(result["adjusted_notional"]) > 0 else 1.0
                )
                result["adjusted_notional"] = max(0.0, guarded)
                projected = projected_post_fill(equity, gross, result["adjusted_notional"], rate)
                result.update(projected)
            except Exception as exc:
                # Fail open to the existing engine-level room clamp if this
                # diagnostic hard-guard layer itself has an unexpected issue.
                result["leverage_guard_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        # Core risk-layer failure also fails open for research continuity.
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["combined_multiplier"] = 1.0
        result["adjusted_notional"] = original
        result["pre_execution_adjusted_notional"] = original

    return result
