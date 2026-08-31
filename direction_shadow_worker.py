from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.decision_engine import HORIZON_SPECS, atr
from src.direction_engine import assess_direction
from src.market_cache import MarketCache
from src.simulation_db import SimulationDB
from src.symbol_strategy_health import find_symbol_strategy_health, symbol_strategy_health_snapshot

PUBLIC_PATH = Path("static") / "direction_shadow_snapshot.json"
REFRESH_SECONDS = 900


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_snapshot(db: SimulationDB, cache: MarketCache) -> dict:
    rows = []
    errors = []
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
                pack = cache.ensure(market, symbol, horizon)
                df = cache.closed_only(pack["data"], market, horizon)
                if len(df) < 80:
                    continue
                a = atr(df, 14)
                px = float(df.close.iloc[-1])
                atr_pct = float(a.iloc[-1] / px) if px > 0 and a.iloc[-1] == a.iloc[-1] else 0.03
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
                    **result,
                })
            except Exception as exc:
                errors.append({"market": market, "symbol": symbol, "horizon": horizon, "error": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda r: (float(r.get("direction_confidence") or 0.0), float(r.get("ev_gap_r") or 0.0)), reverse=True)
    return {
        "generated_at": _now_iso(),
        "scope": "PUBLIC_READ_ONLY_DIRECTION_SHADOW",
        "decision_engine_version": "V10_ADAPTIVE_EVIDENCE_SHADOW",
        "contains_secrets": False,
        "shadow_only": True,
        "short_execution_enabled": False,
        "broker_order_api_calls": 0,
        "rows": rows,
        "summary": {
            "candidates": len(rows),
            "long": sum(1 for r in rows if r.get("direction") == "LONG"),
            "short": sum(1 for r in rows if r.get("direction") == "SHORT"),
            "no_trade": sum(1 for r in rows if r.get("direction") == "NO_TRADE"),
            "errors": len(errors),
            "volume_available": sum(1 for r in rows if (r.get("volume_evidence") or {}).get("status") == "AVAILABLE"),
            "forward_health_linked": sum(1 for r in rows if ((r.get("forward_stability") or {}).get("samples") or 0) > 0),
        },
        "errors": errors[:50],
    }


def write_snapshot(db, cache):
    payload = build_snapshot(db, cache)
    PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PUBLIC_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(PUBLIC_PATH)
    return payload


def main():
    db = SimulationDB("simulation_lab.sqlite3")
    cache = MarketCache("market_cache.sqlite3")
    while True:
        try:
            write_snapshot(db, cache)
        except Exception:
            pass
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
