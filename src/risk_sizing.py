from __future__ import annotations

import os
import math

from .backtest import ExecutionCosts
from .entry_gate import finalize_entry, multiplier
from .data_quality_drift import assess_pair
from .decision_engine import HORIZON_SPECS
from .expected_live_sizing import expected_live_sizing_assessment
from .leverage_guard import cost_aware_leverage_room, projected_post_fill
from .meta_model import meta_entry_assessment
from .portfolio_ev import score_portfolio_candidate
from .pretrade_risk import build_pretrade_risk_snapshot
from .pro_risk_engine import portfolio_risk_snapshot, strategy_health_snapshot
from .symbol_strategy_health import find_symbol_strategy_health, symbol_strategy_health_snapshot
from .trade_ev import estimate_trade_ev


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


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


def _trade_ev_multiplier(expected_value_pct: float, expected_value_r: float, evidence_weight: float) -> tuple[float, str]:
    ev_pct = float(expected_value_pct or 0.0)
    ev_r = float(expected_value_r or 0.0)
    evidence = max(0.0, min(1.0, float(evidence_weight or 0.0)))
    if ev_pct <= 0.0 or ev_r <= 0.0:
        # Immature evidence reduces sizing; finalize_entry vetoes mature negative EV.
        # Independent V10 observations continue without funding a rejected entry.
        return (0.50 if evidence < 0.25 else 0.25), "NEGATIVE_EV"
    if ev_r >= 0.50:
        return 1.00, "STRONG_POSITIVE_EV"
    if ev_r >= 0.25:
        return 0.90, "POSITIVE_EV"
    if ev_r >= 0.10:
        return 0.80, "MODEST_POSITIVE_EV"
    return 0.70, "THIN_POSITIVE_EV"


def _portfolio_ev_multiplier(score: float, positive_ev: bool) -> tuple[float, str]:
    s = float(score or 0.0)
    if not positive_ev:
        return 0.50, "NEGATIVE_EV_PORTFOLIO"
    if s >= 0.45:
        return 1.00, "TOP_TIER"
    if s >= 0.25:
        return 0.90, "HIGH"
    if s >= 0.10:
        return 0.80, "MEDIUM"
    return 0.70, "LOW"


def active_entry_sizing(db, cache, market: str, symbol: str, horizon: str, decision: dict,
                        requested_notional: float) -> dict:
    """Return the notional to use for a virtual BUY fill.

    Independent evidence layers are combined at the last possible moment. Trade EV
    and portfolio EV are active for simulation sizing, while overlapping evidence is
    de-duplicated with min() rather than multiplied repeatedly. Broker orders remain
    disabled; this only changes virtual position size.
    """
    try:
        original = float(requested_notional)
        if not math.isfinite(original) or original < 0:
            raise ValueError("invalid requested notional")
    except (TypeError, ValueError, OverflowError):
        return finalize_entry({"original_notional": 0.0, "adjusted_notional": 0.0,
                               "error": "INVALID_REQUESTED_NOTIONAL"})
    result = {
        "original_notional": original,
        "adjusted_notional": original,
        "pre_execution_adjusted_notional": original,
        "combined_multiplier": 1.0,
        "portfolio_multiplier": 1.0,
        "effective_portfolio_multiplier": 1.0,
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
        "forward_shadow_weight": 0.0,
        "backtest_oos_weight": 1.0,
        "raw_expected_live_multiplier": 1.0,
        "realized_evidence_multiplier": 1.0,
        "realized_evidence_dedup_applied": False,
        "trade_ev_probability_win": None,
        "trade_ev_expected_value_pct": None,
        "trade_ev_expected_value_r": None,
        "trade_ev_evidence_trades": 0,
        "trade_ev_evidence_weight": 0.0,
        "trade_ev_multiplier": 1.0,
        "trade_ev_state": "UNAVAILABLE",
        "portfolio_ev_score": None,
        "portfolio_ev_multiplier": 1.0,
        "portfolio_ev_state": "UNAVAILABLE",
        "portfolio_ev_correlation_penalty": 0.0,
        "portfolio_ev_exposure_penalty": 0.0,
        "alpha_evidence_multiplier": 1.0,
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
        "pretrade_max_correlation": 0.0,
        "pretrade_projected_gross_ratio": 0.0,
        "pretrade_duplicate_symbol": False,
        "flags": "無明顯組合衝突",
        "active_portfolio_sizing": _flag("V6_ACTIVE_PORTFOLIO_SIZING", True),
        "active_trade_ev_sizing": _flag("V6_ACTIVE_TRADE_EV_SIZING", True),
        "active_portfolio_ev_sizing": _flag("V6_ACTIVE_PORTFOLIO_EV_SIZING", True),
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
        "trade_ev_error": None,
        "meta_error": None,
        "quality_error": None,
        "symbol_strategy_error": None,
        "expected_live_error": None,
        "error": None,
    }
    if original <= 0:
        return finalize_entry(result)

    try:
        pretrade_row = None
        if result["active_portfolio_sizing"] or result["active_portfolio_ev_sizing"]:
            pre = build_pretrade_risk_snapshot(db, cache)
            pretrade_row = _find_pretrade_row(pre, market, symbol, horizon)
            if pretrade_row:
                result["pretrade_multiplier"] = multiplier(pretrade_row.get("shadow_size_multiplier", 1.0))
                result["pretrade_verdict"] = str(pretrade_row.get("verdict") or "ALLOW")
                result["pretrade_score"] = float(pretrade_row.get("risk_score", 0.0) or 0.0)
                result["pretrade_max_correlation"] = max(0.0, float(pretrade_row.get("max_correlation", 0.0) or 0.0))
                result["pretrade_projected_gross_ratio"] = max(0.0, float(pretrade_row.get("projected_gross_ratio", 0.0) or 0.0))
                result["pretrade_duplicate_symbol"] = bool(pretrade_row.get("duplicate_symbol", False))
                result["flags"] = str(pretrade_row.get("flags") or "無明顯組合衝突")

        if result["active_portfolio_sizing"]:
            portfolio = portfolio_risk_snapshot(db, cache)
            global_row = next(
                (r for r in (portfolio.get("groups") or []) if str(r.get("group") or "") == "GLOBAL"),
                None,
            )
            if global_row:
                result["global_multiplier"] = multiplier(global_row.get("shadow_risk_multiplier", 1.0))
            result["portfolio_multiplier"] = min(result["pretrade_multiplier"], result["global_multiplier"])

        trade_ev_multiplier = 1.0
        if result["active_trade_ev_sizing"] or result["active_portfolio_ev_sizing"]:
            try:
                model = db.model(market, symbol, horizon) or {}
                model_diag = model.get("diagnostics") if isinstance(model.get("diagnostics"), dict) else {}
                oos_metrics = model_diag.get("oos_metrics") if isinstance(model_diag.get("oos_metrics"), dict) else {}
                decision_diag = decision.get("diagnostics") if isinstance(decision.get("diagnostics"), dict) else {}
                regime_fit = decision_diag.get("regime_fit", model.get("regime_fit", 0.5))
                rate = _cost_rate(market)
                ev = estimate_trade_ev(
                    oos_metrics=oos_metrics,
                    trade_confidence=float(decision.get("confidence", 50.0) or 50.0),
                    regime_fit=float(regime_fit or 0.5),
                    stop_distance=float(decision.get("stop_distance", 0.0) or 0.0),
                    target_distance=float(decision.get("target_distance", 0.0) or 0.0),
                    buy_cost_rate=rate,
                    sell_cost_rate=rate,
                )
                result["trade_ev_probability_win"] = ev.probability_win
                result["trade_ev_expected_value_pct"] = ev.expected_value_pct
                result["trade_ev_expected_value_r"] = ev.expected_value_r
                result["trade_ev_evidence_trades"] = ev.evidence_trades
                result["trade_ev_evidence_weight"] = ev.evidence_weight
                trade_ev_multiplier, result["trade_ev_state"] = _trade_ev_multiplier(
                    ev.expected_value_pct, ev.expected_value_r, ev.evidence_weight
                )
                result["trade_ev_multiplier"] = trade_ev_multiplier

                if result["active_portfolio_ev_sizing"]:
                    corr_penalty = max(0.0, min(1.0, result["pretrade_max_correlation"]))
                    ratio = result["pretrade_projected_gross_ratio"]
                    exposure_penalty = max(0.0, min(1.0, (ratio - 0.35) / 0.65))
                    if result["pretrade_duplicate_symbol"]:
                        exposure_penalty = max(exposure_penalty, 0.50)
                    pscore = score_portfolio_candidate({
                        "symbol": symbol,
                        "market": market,
                        "horizon": horizon,
                        "strategy": str(decision.get("strategy") or ""),
                        "expected_value_pct": ev.expected_value_pct,
                        "expected_value_r": ev.expected_value_r,
                        "evidence_weight": ev.evidence_weight,
                        "confidence": float(decision.get("confidence", 50.0) or 50.0),
                        "correlation_penalty": corr_penalty,
                        "exposure_penalty": exposure_penalty,
                    })
                    result["portfolio_ev_score"] = pscore.portfolio_ev_score
                    result["portfolio_ev_correlation_penalty"] = corr_penalty
                    result["portfolio_ev_exposure_penalty"] = exposure_penalty
                    pem, pstate = _portfolio_ev_multiplier(
                        pscore.portfolio_ev_score,
                        ev.expected_value_pct > 0.0 and ev.expected_value_r > 0.0,
                    )
                    result["portfolio_ev_multiplier"] = pem
                    result["portfolio_ev_state"] = pstate
            except Exception as exc:
                result["trade_ev_error"] = f"{type(exc).__name__}: {exc}"
                trade_ev_multiplier = 1.0

        result["effective_portfolio_multiplier"] = min(
            result["portfolio_multiplier"], result["portfolio_ev_multiplier"]
        )

        health_multiplier = 1.0
        if result["active_strategy_health_sizing"]:
            health = strategy_health_snapshot(db)
            strategy = str(decision.get("strategy") or "")
            regime = str(decision.get("regime") or "")
            srow = _find_health_row(health.get("strategies") or [], market, horizon, strategy)
            rrow = _find_health_row(health.get("regimes") or [], market, horizon, strategy, regime)
            if srow:
                result["strategy_multiplier"] = multiplier(srow.get("shadow_weight_multiplier", 1.0))
                result["strategy_state"] = str(srow.get("state") or "LEARNING")
            if rrow:
                result["regime_multiplier"] = multiplier(rrow.get("shadow_weight_multiplier", 1.0))
                result["regime_state"] = str(rrow.get("state") or "LEARNING")
            health_multiplier = min(result["strategy_multiplier"], result["regime_multiplier"])

        if result["active_symbol_strategy_health_sizing"]:
            try:
                strategy = str(decision.get("strategy") or "")
                symbol_health = symbol_strategy_health_snapshot(db)
                hrow = find_symbol_strategy_health(symbol_health, market, symbol, horizon, strategy)
                if hrow:
                    result["symbol_strategy_multiplier"] = multiplier(hrow.get("shadow_weight_multiplier", 1.0))
                    result["symbol_strategy_state"] = str(hrow.get("state") or "LEARNING")
                    result["symbol_strategy_samples"] = int(hrow.get("samples", 0) or 0)
                    result["symbol_strategy_failure_votes"] = int(hrow.get("failure_votes", 0) or 0)
                    result["symbol_strategy_profit_factor"] = hrow.get("profit_factor")
                    result["symbol_strategy_weighted_win_rate"] = hrow.get("weighted_win_rate")
                    result["symbol_strategy_weighted_avg_return"] = hrow.get("weighted_avg_return")
                    result["symbol_strategy_performance_key"] = hrow.get("performance_key")
                health_multiplier = min(health_multiplier, result["symbol_strategy_multiplier"])
            except Exception as exc:
                result["symbol_strategy_error"] = f"{type(exc).__name__}: {exc}"

        expected_live_multiplier = 1.0
        if result["active_expected_live_sizing"]:
            try:
                strategy = str(decision.get("strategy") or "")
                deviation = expected_live_sizing_assessment(db, market, symbol, horizon, strategy)
                result["expected_live_multiplier"] = multiplier(deviation.get("expected_live_multiplier", 1.0))
                result["expected_live_state"] = str(deviation.get("expected_live_state") or "LEARNING")
                result["expected_live_samples"] = int(deviation.get("expected_live_samples", 0) or 0)
                result["expected_live_deviation_score"] = deviation.get("expected_live_deviation_score")
                result["expected_live_reasons"] = list(deviation.get("expected_live_reasons") or [])
                result["expected_live_performance_key"] = deviation.get("expected_live_performance_key")
                result["expected_live_evidence_weight"] = float(deviation.get("expected_live_evidence_weight", 0.0) or 0.0)
                result["forward_shadow_weight"] = float(deviation.get("forward_shadow_weight", 0.0) or 0.0)
                result["backtest_oos_weight"] = multiplier(deviation.get("backtest_oos_weight", 1.0))
                result["raw_expected_live_multiplier"] = multiplier(deviation.get("raw_expected_live_multiplier", 1.0))
                expected_live_multiplier = result["expected_live_multiplier"]
            except Exception as exc:
                result["expected_live_error"] = f"{type(exc).__name__}: {exc}"
                expected_live_multiplier = 1.0

        meta_multiplier = 1.0
        if result["active_meta_sizing"]:
            try:
                meta = meta_entry_assessment(db, market, symbol, horizon, decision)
                result.update(meta)
                meta_multiplier = multiplier(meta.get("meta_multiplier", 1.0))
            except Exception as exc:
                result["meta_error"] = f"{type(exc).__name__}: {exc}"
                meta_multiplier = 1.0

        quality_multiplier = 1.0
        if result["active_data_quality_sizing"]:
            try:
                health = assess_pair(db, cache, market, symbol, horizon)
                result["quality_drift_multiplier"] = multiplier(health.get("quality_drift_multiplier", 1.0))
                result["data_multiplier"] = multiplier(health.get("data_multiplier", 1.0))
                result["drift_multiplier"] = multiplier(health.get("drift_multiplier", 1.0))
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

        realized_evidence_multiplier = min(health_multiplier, expected_live_multiplier)
        result["realized_evidence_multiplier"] = realized_evidence_multiplier
        result["realized_evidence_dedup_applied"] = (
            health_multiplier < 0.999999 and expected_live_multiplier < 0.999999
        )
        # Trade EV and realized strategy health partially reuse OOS/current-model
        # evidence, so keep the stricter view instead of multiplying them twice.
        result["alpha_evidence_multiplier"] = min(
            realized_evidence_multiplier, result["trade_ev_multiplier"]
        )
        combined = (
            result["effective_portfolio_multiplier"]
            * result["alpha_evidence_multiplier"]
            * meta_multiplier
            * quality_multiplier
        )
        combined = multiplier(combined)
        result["combined_multiplier"] = combined
        result["adjusted_notional"] = original * combined
        result["pre_execution_adjusted_notional"] = result["adjusted_notional"]

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
                result["leverage_guard_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["combined_multiplier"] = 0.0
        result["adjusted_notional"] = 0.0
        result["pre_execution_adjusted_notional"] = 0.0

    return finalize_entry(result)
