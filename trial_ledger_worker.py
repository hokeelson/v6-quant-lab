from __future__ import annotations

from src.worker_progress import public_progress

import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir, db_path
from src.direction_forward import DirectionForwardLedger
from src.entry_gate_audit import entry_gate_audit
from src.execution_audit import execution_audit
from src.trial_ledger import TrialLedger

POLL_SECONDS = 60
DATA_DIR = Path(data_dir())
STATIC_DIR = Path("static")
PUBLIC_SNAPSHOT_PATH = STATIC_DIR / "research_snapshot.json"
ledger = TrialLedger(db_path("trial_ledger.sqlite3"))
direction_ledger = DirectionForwardLedger(db_path("direction_forward.sqlite3"))


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


def _safe_json(value):
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _performance_rows(sim_path: str, by_symbol: bool = False):
    path = Path(sim_path)
    if not path.exists():
        return []
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if "trades" not in _tables(con):
            return []
        symbol_select = "COALESCE(symbol, '') AS symbol," if by_symbol else ""
        symbol_group = ",symbol" if by_symbol else ""
        rows = con.execute(f"""
            SELECT
              CASE
                WHEN account_id LIKE 'crypto_%' THEN 'crypto'
                WHEN account_id LIKE 'stock_%' THEN 'stock'
                WHEN account_id LIKE 'twstock_%' THEN 'twstock'
                ELSE ''
              END AS market,
              {symbol_select}
              COALESCE(horizon, '') AS horizon,
              COALESCE(strategy, '') AS strategy,
              COUNT(*) AS closed_trades,
              SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
              SUM(realized_pnl) AS realized_pnl,
              AVG(return_pct) AS avg_return_pct,
              AVG(realized_pnl) AS avg_pnl,
              MIN(return_pct) AS worst_trade_return,
              MAX(return_pct) AS best_trade_return,
              MAX(exit_bar) AS last_exit_bar,
              SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
              ABS(SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END)) AS gross_loss
            FROM trades
            GROUP BY market{symbol_group},horizon,strategy
            ORDER BY closed_trades DESC, market{symbol_group},horizon,strategy
        """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            n = int(d.get("closed_trades") or 0)
            wins = int(d.get("wins") or 0)
            gp = _finite(d.get("gross_profit")) or 0.0
            gl = _finite(d.get("gross_loss")) or 0.0
            item = {
                "market": d.get("market"),
                "horizon": d.get("horizon"),
                "strategy": d.get("strategy"),
                "closed_trades": n,
                "wins": wins,
                "losses": int(d.get("losses") or 0),
                "win_rate": (wins / n) if n else None,
                "realized_pnl": _finite(d.get("realized_pnl")),
                "avg_return_pct": _finite(d.get("avg_return_pct")),
                "avg_pnl": _finite(d.get("avg_pnl")),
                "profit_factor": (gp / gl) if gl > 0 else (None if gp <= 0 else 999.0),
                "worst_trade_return": _finite(d.get("worst_trade_return")),
                "best_trade_return": _finite(d.get("best_trade_return")),
                "last_exit_bar": d.get("last_exit_bar"),
            }
            if by_symbol:
                item["symbol"] = d.get("symbol")
                item["performance_key"] = f"{item['market']}:{item['symbol']}:{item['horizon']}:{item['strategy']}"
            out.append(item)
        return out
    finally:
        con.close()


def _strategy_performance(sim_path: str):
    return _performance_rows(sim_path, by_symbol=False)


def _strategy_symbol_performance(sim_path: str):
    return _performance_rows(sim_path, by_symbol=True)


def _models(sim_path: str):
    path = Path(sim_path)
    if not path.exists():
        return []
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if "models" not in _tables(con):
            return []
        rows = con.execute("SELECT * FROM models ORDER BY market,symbol,horizon").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            diag = {}
            try:
                diag = json.loads(d.get("diagnostics_json") or "{}")
            except Exception:
                pass
            oos = diag.get("oos_metrics") if isinstance(diag, dict) else {}
            if not isinstance(oos, dict):
                oos = {}
            out.append({
                "market": d.get("market"),
                "symbol": d.get("symbol"),
                "horizon": d.get("horizon"),
                "strategy": d.get("strategy"),
                "calibration_score": _finite(d.get("calibration_score")),
                "oos_score": _finite(d.get("oos_score")),
                "train_score": _finite(d.get("train_score")),
                "regime_fit": _finite(d.get("regime_fit")),
                "calibrated_through": d.get("calibrated_through"),
                "updated_at": d.get("updated_at"),
                "oos_metrics": {
                    "total_return": _finite(oos.get("total_return")),
                    "sharpe": _finite(oos.get("sharpe")),
                    "max_drawdown": _finite(oos.get("max_drawdown")),
                    "closed_trades": int(oos.get("closed_trades", 0) or 0),
                    "win_rate": _finite(oos.get("win_rate")),
                    "profit_factor": _finite(oos.get("profit_factor")),
                },
            })
        return out
    finally:
        con.close()


def _accounts(sim_path: str):
    path = Path(sim_path)
    if not path.exists():
        return []
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        tables = _tables(con)
        if "accounts" not in tables:
            return []
        rows = con.execute("SELECT * FROM accounts ORDER BY market,horizon").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            aid = d.get("account_id")
            latest = None
            if "equity_history" in tables:
                latest = con.execute(
                    "SELECT * FROM equity_history WHERE account_id=? ORDER BY bar_time DESC LIMIT 1", (aid,)
                ).fetchone()
            trades = 0
            if "trades" in tables:
                trades = int(con.execute("SELECT COUNT(*) FROM trades WHERE account_id=?", (aid,)).fetchone()[0])
            equity = _finite(latest["equity"]) if latest else _finite(d.get("cash"))
            initial = _finite(d.get("initial_equity")) or 0.0
            out.append({
                "account_id": aid,
                "market": d.get("market"),
                "horizon": d.get("horizon"),
                "initial_equity": initial,
                "equity": equity,
                "return_pct": ((equity / initial) - 1.0) if equity is not None and initial > 0 else None,
                "cash": _finite(d.get("cash")),
                "drawdown": _finite(latest["drawdown"]) if latest else None,
                "gross_exposure": _finite(latest["gross_exposure"]) if latest else None,
                "leverage": _finite(latest["leverage"]) if latest else None,
                "closed_trades": trades,
                "as_of": latest["bar_time"] if latest else None,
            })
        return out
    finally:
        con.close()


def _governance(governance_path: str):
    path = Path(governance_path)
    if not path.exists():
        return {"arenas": [], "recent_events": []}
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        tables = _tables(con)
        arenas = []
        if "arenas" in tables:
            for r in con.execute("SELECT * FROM arenas ORDER BY registered_at DESC LIMIT 100"):
                d = dict(r)
                arenas.append({
                    "arena_id": d.get("arena_id"),
                    "market": d.get("market"),
                    "symbol": d.get("symbol"),
                    "horizon": d.get("horizon"),
                    "registered_at": d.get("registered_at"),
                    "status": d.get("status"),
                    "latest_verdict": d.get("latest_verdict"),
                    "decision_at": d.get("decision_at"),
                    "champion_signature": d.get("champion_signature"),
                    "challenger_signature": d.get("challenger_signature"),
                })
        events = []
        if "governance_events" in tables:
            for r in con.execute("SELECT * FROM governance_events ORDER BY id DESC LIMIT 100"):
                d = dict(r)
                events.append({k: d.get(k) for k in (
                    "id", "arena_id", "market", "symbol", "horizon", "event_type", "created_at"
                )})
        return {"arenas": arenas, "recent_events": events}
    finally:
        con.close()


def _forward(forward_path: str):
    path = Path(forward_path)
    if not path.exists():
        return {"candidates": []}
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        tables = _tables(con)
        if "candidates" not in tables:
            return {"candidates": []}
        cols = {r[1] for r in con.execute("PRAGMA table_info(candidates)")}
        wanted = [x for x in (
            "candidate_id", "market", "symbol", "strategy", "registered_at", "research_grade",
            "evidence_coverage", "source_stage", "status", "notes"
        ) if x in cols]
        if not wanted:
            return {"candidates": []}
        sql = "SELECT " + ",".join(wanted) + " FROM candidates ORDER BY registered_at DESC LIMIT 200"
        return {"candidates": [dict(r) for r in con.execute(sql)]}
    except Exception as exc:
        return {"candidates": [], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()


def _public_snapshot(worker, quality):
    sim_path = db_path("simulation_lab.sqlite3")
    gov_path = db_path("model_governance.sqlite3")
    forward_path = db_path("forward_validation.sqlite3")
    safe_worker = {}
    for k in (
        "status", "heartbeat_at", "last_cycle_started_at", "last_cycle_finished_at", "assets_checked",
        "bars_processed", "market_data_api_calls", "broker_order_api_calls", "true_errors",
        "risk_layer", "risk_sizing", "data_quality", "data_quality_warnings", "data_quality_critical",
        "concept_drift_pairs", "portfolio_risk", "realtime_watchlist_sync", "realtime_watchlist_total"
    ):
        if isinstance(worker, dict) and k in worker:
            safe_worker[k] = worker.get(k)
    safe_worker.update(public_progress(worker))
    safe_quality = {}
    for k in ("status", "warnings", "critical_data", "drifted", "errors", "updated_at", "checked_at"):
        if isinstance(quality, dict) and k in quality:
            safe_quality[k] = quality.get(k)

    return _safe_json({
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "PUBLIC_READ_ONLY_RESEARCH_SUMMARY",
        "contains_secrets": False,
        "worker": safe_worker,
        "trial_ledger": ledger.summary(),
        "direction_shadow": direction_ledger.summary(),
        "entry_gate": entry_gate_audit(sim_path),
        "execution_audit": execution_audit(sim_path),
        "accounts": _accounts(sim_path),
        "strategy_performance": _strategy_performance(sim_path),
        "strategy_symbol_performance": _strategy_symbol_performance(sim_path),
        "models": _models(sim_path),
        "governance": _governance(gov_path),
        "forward_validation": _forward(forward_path),
        "data_quality": safe_quality,
    })


def _write_public_snapshot(worker, quality):
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    payload = _public_snapshot(worker, quality)
    tmp = PUBLIC_SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PUBLIC_SNAPSHOT_PATH)


print("V6 Trial Ledger worker started. Side-channel audit only.", flush=True)
while True:
    try:
        gov = ledger.sync_governance(db_path("model_governance.sqlite3"))
        worker = _read_json(DATA_DIR / "worker_status.json")
        quality = _read_json(DATA_DIR / "data_quality_status.json")
        ledger.sync_worker_cycle(worker, quality)
        _write_public_snapshot(worker, quality)
        print("TRIAL_LEDGER_SYNC", gov, ledger.summary(), "PUBLIC_SNAPSHOT_OK", flush=True)
    except Exception as exc:
        print("TRIAL_LEDGER_ERROR", type(exc).__name__, exc, flush=True)
    time.sleep(POLL_SECONDS)
