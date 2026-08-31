from __future__ import annotations

from datetime import datetime, timezone

from .decision_engine import HORIZON_SPECS, atr
from .direction_engine import assess_direction
from .market_cache import TIMEFRAME_MAP
from .symbol_strategy_health import find_symbol_strategy_health, symbol_strategy_health_snapshot


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_cached_direction_fallback(db, cache) -> dict:
    """Build LONG/SHORT/NO_TRADE rows using only already-cached OHLCV.

    This deliberately never calls MarketCache.ensure(), so opening the dashboard
    cannot create additional external market-data API traffic.
    """
    rows: list[dict] = []
    errors: list[dict] = []
    try:
        health_snapshot = symbol_strategy_health_snapshot(db)
    except Exception as exc:
        health_snapshot = {"symbols": [], "shadow_only": True}
        errors.append({"component": "symbol_strategy_health", "error": f"{type(exc).__name__}: {exc}"})
    for asset in db.assets():
        market = str(asset.get("market") or "")
        symbol = str(asset.get("symbol") or "").upper()
        if market not in ("stock", "crypto") or not symbol:
            continue
        for horizon in ("short", "medium", "long"):
            try:
                model = db.model(market, symbol, horizon)
                if not model:
                    continue
                alpaca_tf, binance_tf = TIMEFRAME_MAP[(market, horizon)]
                timeframe = alpaca_tf if market == "stock" else binance_tf
                df = cache.get(market, symbol, timeframe)
                df = cache.closed_only(df, market, horizon)
                if df is None or len(df) < 80:
                    continue
                a = atr(df, 14)
                px = float(df.close.iloc[-1])
                atr_value = a.iloc[-1]
                atr_pct = float(atr_value / px) if px > 0 and atr_value == atr_value else 0.03
                spec = HORIZON_SPECS[horizon]
                stop = max(0.01, min(0.30, float(spec["atr_stop"]) * atr_pct))
                target = max(0.02, min(0.80, float(spec["atr_target"]) * atr_pct))
                strategy = str(model.get("strategy") or "")
                performance_health = find_symbol_strategy_health(
                    health_snapshot, market, symbol, horizon, strategy
                ) or {}
                diagnostics = model.get("diagnostics") or {}
                performance_health = {
                    **performance_health,
                    "model_stability": diagnostics.get("stability", 50.0),
                    "model_sample": diagnostics.get("sample", 0.0),
                }
                result = assess_direction(
                    df, market, strategy, stop, target,
                    performance_health=performance_health,
                )
                rows.append({
                    "market": market,
                    "symbol": symbol,
                    "horizon": horizon,
                    "strategy": model.get("strategy"),
                    "as_of": df.index[-1].isoformat(),
                    "close": px,
                    "stop_distance": stop,
                    "target_distance": target,
                    "source": "dashboard_cached_fallback",
                    **result,
                })
            except Exception as exc:
                errors.append({
                    "market": market,
                    "symbol": symbol,
                    "horizon": horizon,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    rows.sort(
        key=lambda r: (
            float(r.get("direction_confidence") or 0.0),
            float(r.get("ev_gap_r") or 0.0),
        ),
        reverse=True,
    )
    return {
        "generated_at": _now_iso(),
        "scope": "DASHBOARD_CACHED_DIRECTION_FALLBACK",
        "decision_engine_version": "V10_ADAPTIVE_EVIDENCE_SHADOW",
        "shadow_only": True,
        "short_execution_enabled": False,
        "broker_order_api_calls": 0,
        "rows": rows,
        "errors": errors[:50],
    }
