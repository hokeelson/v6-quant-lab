from __future__ import annotations

from .backtest import ExecutionCosts
from .batch_portfolio_optimizer import optimize_batch
from .decision_engine import regime_fit
from .regime_allocation import regime_strategy_multiplier
from .trade_ev import estimate_trade_ev


def _cost_rate(market: str) -> float:
    costs = ExecutionCosts(0, 3, 2) if market == "stock" else ExecutionCosts(10, 5, 4)
    return float(costs.one_way_rate)


def apply_pretrade_batch_overlay(db, cache, rows: list[dict], correlation_fn) -> list[dict]:
    """Attach dynamic regime and same-batch portfolio EV sizing to pre-trade rows."""
    if not rows:
        return rows

    by_market: dict[str, list[dict]] = {}
    for row in rows:
        market = str(row.get("market") or "")
        symbol = str(row.get("symbol") or "").upper()
        horizon = str(row.get("horizon") or "")
        strategy = str(row.get("strategy") or "")
        regime = str(row.get("regime") or "UNKNOWN")
        key = f"{market}:{symbol}:{horizon}"

        regime_mult = regime_strategy_multiplier(strategy, regime)
        row["dynamic_regime_multiplier"] = regime_mult
        row["dynamic_regime"] = regime

        model = db.model(market, symbol, horizon)
        diagnostics = (model or {}).get("diagnostics") or {}
        oos_metrics = diagnostics.get("oos_metrics") or {}
        rf = regime_fit(strategy, regime)
        rate = _cost_rate(market)
        ev = estimate_trade_ev(
            oos_metrics=oos_metrics,
            trade_confidence=float(row.get("trade_confidence") or 0.0),
            regime_fit=rf,
            stop_distance=float(row.get("stop_distance") or 0.0),
            target_distance=float(row.get("target_distance") or 0.0),
            buy_cost_rate=rate,
            sell_cost_rate=rate,
        ).as_dict()
        row["trade_ev"] = ev

        projected = float(row.get("projected_gross_ratio") or 0.0)
        exposure_penalty = max(0.0, min(1.0, (projected - 0.50) / 0.75))
        corr_penalty = max(0.0, min(1.0, float(row.get("max_correlation") or 0.0)))
        by_market.setdefault(market, []).append({
            "candidate_key": key,
            "market": market,
            "symbol": symbol,
            "horizon": horizon,
            "strategy": strategy,
            "expected_value_pct": ev.get("expected_value_pct", 0.0),
            "expected_value_r": ev.get("expected_value_r", 0.0),
            "evidence_weight": ev.get("evidence_weight", 0.0),
            "confidence": float(row.get("trade_confidence") or 0.0),
            "correlation_penalty": corr_penalty,
            "exposure_penalty": exposure_penalty,
            "requested_notional": float(row.get("requested_notional") or 0.0),
        })

    allocations: dict[str, dict] = {}
    for market, candidates in by_market.items():
        symbols = sorted({str(c.get("symbol") or "").upper() for c in candidates})
        pairwise: dict[tuple[str, str], float] = {}
        for i, a in enumerate(symbols):
            for b in symbols[i + 1:]:
                corr, samples = correlation_fn(cache, market, a, b)
                if corr is not None and samples >= 40:
                    pairwise[tuple(sorted((a, b)))] = max(0.0, float(corr))
        allocations.update(optimize_batch(candidates, pairwise_correlation=pairwise))

    for row in rows:
        key = f"{row.get('market')}:{str(row.get('symbol') or '').upper()}:{row.get('horizon')}"
        batch = allocations.get(key, {
            "batch_ev_rank": None,
            "batch_ev_multiplier": 1.0,
            "batch_ev_verdict": "NO_BATCH_DATA",
            "batch_ev_score": None,
            "batch_ev_expected_value_pct": None,
            "batch_ev_expected_value_r": None,
            "batch_ev_cohort_max_correlation": 0.0,
        })
        row.update(batch)
        # Existing pretrade risk, regime prior, and batch EV overlap on exposure/
        # correlation evidence. Use the strictest multiplier instead of multiplying.
        row["shadow_size_multiplier"] = min(
            float(row.get("shadow_size_multiplier", 1.0) or 1.0),
            float(row.get("dynamic_regime_multiplier", 1.0) or 1.0),
            float(row.get("batch_ev_multiplier", 1.0) or 1.0),
        )
    return rows
