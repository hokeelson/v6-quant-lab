from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from src.expected_live_deviation import expected_live_deviation_snapshot
from src.paths import data_dir
from src.simulation_db import SimulationDB

POLL_SECONDS = 5
EXPECTED_LIVE_REFRESH_SECONDS = 60
DATA_DIR = Path(data_dir())
STATUS_PATH = DATA_DIR / "storage_persistence_status.json"
SIM_PATH = DATA_DIR / "simulation_lab.sqlite3"
PUBLIC_STATUS_PATH = Path("static") / "storage_persistence.json"
PUBLIC_SIZING_PATH = Path("static") / "risk_sizing_audit.json"
PUBLIC_EXPECTED_LIVE_PATH = Path("static") / "expected_live_deviation.json"
_last_expected_live_export = 0.0

SAFE_KEYS = (
    "mode",
    "persistence_status",
    "last_snapshot_at",
    "last_snapshot_success",
    "last_snapshot_failed",
    "snapshot_interval_seconds",
    "snapshot_db_count",
    "snapshot_keep_archives",
    "skipped_rebuildable",
    "persistent_disk",
    "bootstrap_at",
    "restored",
    "bootstrap_warnings",
    "updated_at",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _mult(payload: dict, key: str, default: float = 1.0) -> float:
    x = _finite(payload.get(key))
    return default if x is None else max(0.0, min(1.0, x))


def _safe_list(value, limit: int = 20):
    if isinstance(value, (list, tuple)):
        return [str(x)[:200] for x in value[:limit]]
    if value in (None, ""):
        return []
    return [str(value)[:200]]


def _market_from_account(account_id: str) -> str:
    aid = str(account_id or "")
    if aid.startswith("twstock_"):
        return "twstock"
    if aid.startswith("crypto_"):
        return "crypto"
    if aid.startswith("stock_"):
        return "stock"
    return ""


def _safe_storage_status(raw):
    if not isinstance(raw, dict):
        return {
            "status": "UNAVAILABLE",
            "persistence_status": "UNKNOWN",
        }
    out = {k: raw.get(k) for k in SAFE_KEYS if k in raw}
    out["status"] = "AVAILABLE"
    return out


def _sizing_audit(limit: int = 100):
    empty = {
        "status": "UNAVAILABLE",
        "scope": "PUBLIC_READ_ONLY_RISK_SIZING_AUDIT",
        "contains_secrets": False,
        "generated_at": _now_iso(),
        "summary": {
            "entries": 0,
            "expected_live_reduced": 0,
            "broker_order_api_calls": 0,
        },
        "entries": [],
    }
    if not SIM_PATH.exists():
        return empty

    try:
        con = sqlite3.connect(f"file:{SIM_PATH}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "diagnostics" not in tables:
            con.close()
            return empty

        has_orders = "orders" in tables
        has_decisions = "decisions" in tables
        if has_orders and has_decisions:
            sql = """
                SELECT d.id,d.account_id,d.symbol,d.horizon,d.bar_time,d.created_at,d.payload_json,
                       dec.strategy AS strategy,dec.regime AS regime
                FROM diagnostics d
                LEFT JOIN orders o
                  ON o.account_id=d.account_id AND o.symbol=d.symbol
                 AND o.filled_bar=d.bar_time AND o.side='BUY'
                LEFT JOIN decisions dec ON dec.decision_id=o.decision_id
                WHERE d.category='RISK_SIZING'
                ORDER BY d.id DESC LIMIT ?
            """
        else:
            sql = """
                SELECT d.id,d.account_id,d.symbol,d.horizon,d.bar_time,d.created_at,d.payload_json,
                       NULL AS strategy,NULL AS regime
                FROM diagnostics d
                WHERE d.category='RISK_SIZING'
                ORDER BY d.id DESC LIMIT ?
            """

        rows = con.execute(sql, (int(limit),)).fetchall()
        con.close()
    except Exception:
        return empty

    entries = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        original = _finite(payload.get("original_notional")) or 0.0
        adjusted = _finite(payload.get("adjusted_notional"))
        if adjusted is None:
            adjusted = original
        pre_execution_adjusted = _finite(payload.get("pre_execution_adjusted_notional"))
        if pre_execution_adjusted is None:
            pre_execution_adjusted = adjusted
        filled = _finite(payload.get("filled_notional"))
        if filled is None:
            filled = adjusted

        strategy_mult = _mult(payload, "strategy_multiplier")
        regime_mult = _mult(payload, "regime_multiplier")
        symbol_mult = _mult(payload, "symbol_strategy_multiplier")
        expected_live_mult = _mult(payload, "expected_live_multiplier")
        broad_health_mult = min(strategy_mult, regime_mult)
        effective_health_mult = min(broad_health_mult, symbol_mult)
        leverage_guard_mult = _mult(payload, "leverage_guard_multiplier")
        execution_cap_mult = min(1.0, max(0.0, filled / adjusted)) if adjusted > 0 else 1.0
        final_effective_mult = min(1.0, max(0.0, filled / original)) if original > 0 else 1.0

        room = _finite(payload.get("leverage_room"))
        room_type = "LEVERAGE"
        if room is None:
            room = _finite(payload.get("cash_room"))
            room_type = "CASH" if room is not None else "UNKNOWN"

        flags = payload.get("flags")
        if isinstance(flags, (list, tuple)):
            flags = [str(x) for x in flags[:20]]
        elif flags is not None:
            flags = str(flags)[:500]

        item = {
            "id": int(row["id"]),
            "account_id": row["account_id"],
            "market": _market_from_account(row["account_id"]),
            "symbol": row["symbol"],
            "horizon": row["horizon"],
            "strategy": row["strategy"],
            "regime": row["regime"],
            "bar_time": row["bar_time"],
            "created_at": row["created_at"],
            "original_notional": original,
            "portfolio_multiplier": _mult(payload, "portfolio_multiplier"),
            "pretrade_multiplier": _mult(payload, "pretrade_multiplier"),
            "global_multiplier": _mult(payload, "global_multiplier"),
            "strategy_multiplier": strategy_mult,
            "regime_multiplier": regime_mult,
            "broad_health_multiplier": broad_health_mult,
            "symbol_strategy_multiplier": symbol_mult,
            "effective_health_multiplier": effective_health_mult,
            "symbol_strategy_state": str(payload.get("symbol_strategy_state") or "LEARNING"),
            "symbol_strategy_samples": int(payload.get("symbol_strategy_samples", 0) or 0),
            "symbol_strategy_failure_votes": int(payload.get("symbol_strategy_failure_votes", 0) or 0),
            "symbol_strategy_profit_factor": _finite(payload.get("symbol_strategy_profit_factor")),
            "symbol_strategy_weighted_win_rate": _finite(payload.get("symbol_strategy_weighted_win_rate")),
            "symbol_strategy_weighted_avg_return": _finite(payload.get("symbol_strategy_weighted_avg_return")),
            "expected_live_multiplier": expected_live_mult,
            "expected_live_state": str(payload.get("expected_live_state") or "LEARNING"),
            "expected_live_samples": int(payload.get("expected_live_samples", 0) or 0),
            "expected_live_deviation_score": _finite(payload.get("expected_live_deviation_score")),
            "expected_live_evidence_weight": _finite(payload.get("expected_live_evidence_weight")) or 0.0,
            "expected_live_performance_key": str(payload.get("expected_live_performance_key") or ""),
            "expected_live_reasons": _safe_list(payload.get("expected_live_reasons")),
            "meta_multiplier": _mult(payload, "meta_multiplier"),
            "meta_score": _finite(payload.get("meta_score")),
            "meta_probability": _finite(payload.get("meta_probability")),
            "meta_verdict": str(payload.get("meta_verdict") or "LEARNING"),
            "meta_mode": str(payload.get("meta_mode") or "COLD_START"),
            "meta_samples": int(payload.get("meta_samples", 0) or 0),
            "meta_tca_samples": int(payload.get("meta_tca_samples", 0) or 0),
            "meta_spread_bps": _finite(payload.get("meta_spread_bps")),
            "quality_drift_multiplier": _mult(payload, "quality_drift_multiplier"),
            "data_multiplier": _mult(payload, "data_multiplier"),
            "drift_multiplier": _mult(payload, "drift_multiplier"),
            "data_status": str(payload.get("data_status") or "UNKNOWN"),
            "drift_status": str(payload.get("drift_status") or "LEARNING"),
            "pretrade_verdict": str(payload.get("pretrade_verdict") or "ALLOW"),
            "pretrade_score": _finite(payload.get("pretrade_score")),
            "flags": flags,
            "combined_multiplier": _mult(payload, "combined_multiplier"),
            "risk_adjusted_notional": pre_execution_adjusted,
            "leverage_guard_multiplier": leverage_guard_mult,
            "leverage_guard_applied": bool(payload.get("leverage_guard_applied", False)),
            "leverage_guarded_notional": adjusted,
            "legacy_leverage_room": _finite(payload.get("legacy_leverage_room")),
            "cost_adjusted_leverage_room": _finite(payload.get("cost_adjusted_leverage_room")),
            "max_leverage": _finite(payload.get("max_leverage")),
            "target_leverage_cap": _finite(payload.get("target_leverage_cap")),
            "projected_post_fill_leverage": _finite(payload.get("projected_post_fill_leverage")),
            "projected_post_fill_equity": _finite(payload.get("projected_post_fill_equity")),
            "projected_post_fill_gross": _finite(payload.get("projected_post_fill_gross")),
            "execution_room": room,
            "execution_room_type": room_type,
            "execution_cap_multiplier": execution_cap_mult,
            "filled_notional": filled,
            "final_effective_multiplier": final_effective_mult,
            "risk_reduction_notional": max(0.0, original - pre_execution_adjusted),
            "leverage_guard_reduction_notional": max(0.0, pre_execution_adjusted - adjusted),
            "execution_reduction_notional": max(0.0, adjusted - filled),
            "total_reduction_notional": max(0.0, original - filled),
            "fill_price": _finite(payload.get("fill_price")),
            "broker_order_api_calls": int(payload.get("broker_order_api_calls", 0) or 0),
            "has_error": bool(
                payload.get("error") or payload.get("meta_error") or payload.get("quality_error")
                or payload.get("symbol_strategy_error") or payload.get("expected_live_error")
                or payload.get("leverage_guard_error")
            ),
        }
        entries.append(item)

    def reduced(key):
        return sum(1 for x in entries if (_finite(x.get(key)) or 1.0) < 0.999999)

    return {
        "status": "AVAILABLE",
        "scope": "PUBLIC_READ_ONLY_RISK_SIZING_AUDIT",
        "contains_secrets": False,
        "generated_at": _now_iso(),
        "summary": {
            "entries": len(entries),
            "final_size_reduced": reduced("final_effective_multiplier"),
            "portfolio_reduced": reduced("portfolio_multiplier"),
            "broad_health_reduced": reduced("broad_health_multiplier"),
            "symbol_strategy_reduced": reduced("symbol_strategy_multiplier"),
            "expected_live_reduced": reduced("expected_live_multiplier"),
            "meta_reduced": reduced("meta_multiplier"),
            "quality_drift_reduced": reduced("quality_drift_multiplier"),
            "leverage_guard_reduced": reduced("leverage_guard_multiplier"),
            "execution_cap_reduced": reduced("execution_cap_multiplier"),
            "entries_with_error": sum(1 for x in entries if x.get("has_error")),
            "broker_order_api_calls": sum(int(x.get("broker_order_api_calls", 0) or 0) for x in entries),
        },
        "entries": entries,
    }


def _expected_live():
    empty = {
        "status": "UNAVAILABLE",
        "scope": "PUBLIC_READ_ONLY_EXPECTED_LIVE_DEVIATION",
        "contains_secrets": False,
        "generated_at": _now_iso(),
        "shadow_only": True,
        "active_sizing": False,
        "summary": {"models": 0, "with_live_trades": 0, "broker_order_api_calls": 0},
        "rows": [],
    }
    if not SIM_PATH.exists():
        return empty
    try:
        snap = expected_live_deviation_snapshot(SimulationDB(str(SIM_PATH)))
        if not isinstance(snap, dict):
            return empty
        snap["scope"] = "PUBLIC_READ_ONLY_EXPECTED_LIVE_DEVIATION"
        snap["contains_secrets"] = False
        snap["generated_at"] = _now_iso()
        return snap
    except Exception as exc:
        empty["error"] = f"{type(exc).__name__}: {exc}"
        return empty


def _atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def export_once():
    global _last_expected_live_export
    _atomic_json(PUBLIC_STATUS_PATH, _safe_storage_status(_read_json(STATUS_PATH)))
    _atomic_json(PUBLIC_SIZING_PATH, _sizing_audit(100))
    now = time.monotonic()
    if (not PUBLIC_EXPECTED_LIVE_PATH.exists()) or (now - _last_expected_live_export >= EXPECTED_LIVE_REFRESH_SECONDS):
        _atomic_json(PUBLIC_EXPECTED_LIVE_PATH, _expected_live())
        _last_expected_live_export = now
    return True


def watch():
    time.sleep(2)
    while True:
        try:
            export_once()
            print("STORAGE_SIZING_EXPECTED_LIVE_EXPORT OK", flush=True)
        except Exception as exc:
            print("STORAGE_STATUS_EXPORT_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    watch()
