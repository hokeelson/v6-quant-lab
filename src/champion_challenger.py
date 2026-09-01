from __future__ import annotations

from .worker_progress import notify_progress

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import ExecutionCosts, RiskRules, run_backtest
from .decision_engine import HORIZON_SPECS
from .metrics import performance_metrics
from .research import strategy_signal

TW_MARKET = "twstock"
TW_BARS_PER_YEAR = {"short": 1134, "medium": 252, "long": 52}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value):
    """Convert model diagnostics into strict JSON without hiding NaN/NumPy types."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        x = float(value)
        return x if math.isfinite(x) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json(value) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _safe_float(value, default=None):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def model_signature(model: dict) -> str:
    """Fingerprint only strategy + frozen parameters, not changing diagnostics."""
    payload = {
        "strategy": str(model.get("strategy") or ""),
        "params": model.get("params") or {},
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:20]


def _costs_and_bpy(market: str, horizon: str):
    if market == "stock":
        return ExecutionCosts(0, 3, 2), int(HORIZON_SPECS[horizon]["bars_per_year_stock"])
    if market == "crypto":
        return ExecutionCosts(10, 5, 4), int(HORIZON_SPECS[horizon]["bars_per_year_crypto"])
    if market == TW_MARKET:
        return ExecutionCosts(29.25, 5.0, 4.0), int(TW_BARS_PER_YEAR[horizon])
    raise ValueError(f"Unsupported market: {market}")


def _paired_block_probability(champion_returns, challenger_returns, seed_text: str):
    """Paired moving-block bootstrap P(mean challenger return > champion return)."""
    a = np.asarray(champion_returns, dtype=float)
    b = np.asarray(challenger_returns, dtype=float)
    n = min(len(a), len(b))
    if n < 30:
        return None
    diff = b[-n:] - a[-n:]
    diff = diff[np.isfinite(diff)]
    n = len(diff)
    if n < 30:
        return None
    block = max(2, min(12, int(round(math.sqrt(n)))))
    boots = max(200, _int_env("V6_CC_BOOTSTRAP_SAMPLES", 500))
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(boots):
        sample = []
        while len(sample) < n:
            start = int(rng.integers(0, n))
            for j in range(block):
                sample.append(diff[(start + j) % n])
                if len(sample) >= n:
                    break
        means.append(float(np.mean(sample)))
    return float(np.mean(np.asarray(means) > 0.0))


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS research_state(
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL,
  last_research_at TEXT,
  last_candidate_signature TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(market,symbol,horizon)
);

CREATE TABLE IF NOT EXISTS arenas(
  arena_id TEXT PRIMARY KEY,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  initial_capital REAL NOT NULL,
  champion_signature TEXT NOT NULL,
  challenger_signature TEXT NOT NULL,
  champion_model_json TEXT NOT NULL,
  challenger_model_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  latest_verdict TEXT NOT NULL DEFAULT 'WAITING',
  latest_gate_json TEXT,
  decision_at TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_pair_status ON arenas(market,symbol,horizon,status);

CREATE TABLE IF NOT EXISTS arena_snapshots(
  arena_id TEXT NOT NULL,
  role TEXT NOT NULL,
  last_forward_bar TEXT NOT NULL,
  forward_bars INTEGER NOT NULL,
  forward_days INTEGER NOT NULL,
  closed_trades INTEGER NOT NULL,
  total_return REAL,
  sharpe REAL,
  max_drawdown REAL,
  win_rate REAL,
  profit_factor REAL,
  metrics_json TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY(arena_id,role,last_forward_bar)
);

CREATE TABLE IF NOT EXISTS governance_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  arena_id TEXT,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL,
  event_type TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL
);
"""


class ChampionChallenger:
    """Forward-only governance for production Champion vs frozen Challenger.

    Both arms start at the same registration time, use the same future closed bars,
    the same standalone capital/cost/risk rules, and never count pre-registration
    bars as evidence. Pre-registration history is indicator warm-up only.
    """

    def __init__(self, path: str, initial_capital: float = 100000.0):
        self.path = str(path)
        self.initial_capital = float(initial_capital)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.executescript(SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _event(self, arena_id, market, symbol, horizon, event_type, detail=None, now=None):
        with self._c() as c:
            c.execute(
                "INSERT INTO governance_events(arena_id,market,symbol,horizon,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (arena_id, market, symbol, horizon, event_type, _json(detail or {}), now or _now_iso()),
            )

    def mark_research(self, market: str, symbol: str, horizon: str, signature: str | None = None, now=None):
        now = now or _now_iso()
        with self._c() as c:
            c.execute("""
              INSERT INTO research_state(market,symbol,horizon,last_research_at,last_candidate_signature,updated_at)
              VALUES(?,?,?,?,?,?)
              ON CONFLICT(market,symbol,horizon) DO UPDATE SET
                last_research_at=excluded.last_research_at,
                last_candidate_signature=COALESCE(excluded.last_candidate_signature,research_state.last_candidate_signature),
                updated_at=excluded.updated_at
            """, (market, symbol.upper(), horizon, now, signature, now))

    def last_research_at(self, market: str, symbol: str, horizon: str):
        with self._c() as c:
            r = c.execute(
                "SELECT last_research_at FROM research_state WHERE market=? AND symbol=? AND horizon=?",
                (market, symbol.upper(), horizon),
            ).fetchone()
            return r[0] if r else None

    def active_arena(self, market: str, symbol: str, horizon: str):
        with self._c() as c:
            r = c.execute("""
              SELECT * FROM arenas WHERE market=? AND symbol=? AND horizon=? AND status='ACTIVE'
              ORDER BY registered_at DESC LIMIT 1
            """, (market, symbol.upper(), horizon)).fetchone()
            return dict(r) if r else None

    def arenas(self, status: str | None = None):
        with self._c() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM arenas WHERE status=? ORDER BY registered_at DESC", (status,)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM arenas ORDER BY registered_at DESC").fetchall()
            return [dict(r) for r in rows]

    def register_challenge(self, champion_model: dict, challenger_model: dict, now=None):
        now = now or _now_iso()
        market = str(challenger_model.get("market") or champion_model.get("market") or "")
        symbol = str(challenger_model.get("symbol") or champion_model.get("symbol") or "").upper()
        horizon = str(challenger_model.get("horizon") or champion_model.get("horizon") or "")
        csig = model_signature(champion_model)
        nsig = model_signature(challenger_model)

        existing = self.active_arena(market, symbol, horizon)
        if existing:
            return {"status": "ACTIVE_EXISTS", "arena_id": existing["arena_id"]}

        self.mark_research(market, symbol, horizon, nsig, now)
        if csig == nsig:
            self._event(None, market, symbol, horizon, "SAME_HYPOTHESIS_REFRESH", {"signature": nsig}, now)
            return {"status": "SAME_MODEL", "arena_id": None, "signature": nsig}

        raw = f"{market}|{symbol}|{horizon}|{now}|{csig}|{nsig}"
        arena_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        with self._c() as c:
            c.execute("""
              INSERT INTO arenas(
                arena_id,market,symbol,horizon,registered_at,initial_capital,
                champion_signature,challenger_signature,champion_model_json,challenger_model_json,
                status,latest_verdict,latest_gate_json,decision_at,notes,updated_at
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                arena_id, market, symbol, horizon, now, self.initial_capital,
                csig, nsig, _json(champion_model), _json(challenger_model),
                "ACTIVE", "WAITING", _json({"reason": "awaiting_post_registration_bars"}), None,
                "Frozen paired forward comparison. Pre-registration bars never count as evidence.", now,
            ))
        self._event(arena_id, market, symbol, horizon, "CHALLENGE_REGISTERED", {
            "champion_strategy": champion_model.get("strategy"),
            "challenger_strategy": challenger_model.get("strategy"),
            "champion_signature": csig,
            "challenger_signature": nsig,
        }, now)
        return {"status": "REGISTERED", "arena_id": arena_id, "signature": nsig}

    def _latest_snapshot(self, arena_id: str, role: str):
        with self._c() as c:
            r = c.execute("""
              SELECT * FROM arena_snapshots WHERE arena_id=? AND role=?
              ORDER BY computed_at DESC LIMIT 1
            """, (arena_id, role)).fetchone()
            return dict(r) if r else None

    def _save_snapshot(self, arena_id: str, role: str, metrics: dict, now: str):
        with self._c() as c:
            c.execute("""
              INSERT OR REPLACE INTO arena_snapshots(
                arena_id,role,last_forward_bar,forward_bars,forward_days,closed_trades,
                total_return,sharpe,max_drawdown,win_rate,profit_factor,metrics_json,computed_at
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                arena_id, role, metrics["last_forward_bar"], int(metrics["forward_bars"]),
                int(metrics["forward_days"]), int(metrics["closed_trades"]),
                metrics.get("total_return"), metrics.get("sharpe"), metrics.get("max_drawdown"),
                metrics.get("win_rate"), metrics.get("profit_factor"), _json(metrics["metrics"]), now,
            ))

    def _evaluate_model(self, arena: dict, model: dict, df: pd.DataFrame):
        registered = pd.Timestamp(arena["registered_at"])
        registered = registered.tz_localize("UTC") if registered.tzinfo is None else registered.tz_convert("UTC")
        data = df.copy().dropna()
        idx = pd.DatetimeIndex(data.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        data.index = idx
        forward = data[data.index > registered]
        if len(forward) < 2:
            return None

        # Compute indicators on full history for warmup, but execute/evaluate only
        # bars strictly after registration.
        sig_all = strategy_signal(str(model.get("strategy") or ""), data, model.get("params") or {})
        sig = sig_all.reindex(forward.index).fillna(0.0)
        costs, bpy = _costs_and_bpy(str(arena["market"]), str(arena["horizon"]))
        risk = RiskRules(max_position_pct=0.25, stop_loss_pct=0.12, take_profit_pct=0.30)
        result = run_backtest(forward, sig, float(arena["initial_capital"]), costs, risk, bpy, 0.0)
        trades = result["trades"].copy()
        if not trades.empty and "reason" in trades.columns:
            genuine = trades[trades["reason"].fillna("") != "FINAL_LIQUIDATION"].copy()
        else:
            genuine = trades
        m = performance_metrics(result["equity"], genuine, bpy, 0.0)
        first, last = forward.index[0], forward.index[-1]
        days = max(1, int(math.floor((last - first).total_seconds() / 86400.0)) + 1)
        rets = result["equity"].pct_change().replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)

        def clean(name, default=None):
            return _safe_float(m.get(name), default)

        clean_metrics = {}
        for k, v in m.items():
            if isinstance(v, (int, np.integer)):
                clean_metrics[k] = int(v)
            elif isinstance(v, (float, np.floating)):
                clean_metrics[k] = _safe_float(v)
            else:
                clean_metrics[k] = _json_safe(v)

        return {
            "last_forward_bar": last.isoformat(),
            "forward_bars": int(len(forward)),
            "forward_days": days,
            "closed_trades": int(m.get("closed_trades", 0) or 0),
            "total_return": clean("total_return", 0.0),
            "sharpe": clean("sharpe"),
            "max_drawdown": clean("max_drawdown", 0.0),
            "win_rate": clean("win_rate"),
            "profit_factor": clean("profit_factor"),
            "metrics": clean_metrics,
            "returns": rets,
        }

    def _gate(self, arena: dict, champion: dict, challenger: dict):
        min_days = max(1, _int_env("V6_CC_MIN_FORWARD_DAYS", 60))
        min_trades = max(1, _int_env("V6_CC_MIN_CLOSED_TRADES", 20))
        max_days = max(min_days, _int_env("V6_CC_MAX_FORWARD_DAYS", 180))
        min_return = _float_env("V6_CC_MIN_RETURN", 0.0)
        min_sharpe = _float_env("V6_CC_MIN_SHARPE", 0.5)
        min_mdd = _float_env("V6_CC_MIN_MAX_DRAWDOWN", -0.25)
        return_edge = _float_env("V6_CC_RETURN_EDGE", 0.01)
        sharpe_edge = _float_env("V6_CC_SHARPE_EDGE", 0.10)
        dd_slack = abs(_float_env("V6_CC_DRAWDOWN_SLACK", 0.03))
        min_boot = _float_env("V6_CC_MIN_BOOTSTRAP_PROB", 0.90)

        c_sh = _safe_float(champion.get("sharpe"), -99.0)
        n_sh = _safe_float(challenger.get("sharpe"), -99.0)
        c_ret = _safe_float(champion.get("total_return"), -1.0)
        n_ret = _safe_float(challenger.get("total_return"), -1.0)
        c_dd = _safe_float(champion.get("max_drawdown"), -1.0)
        n_dd = _safe_float(challenger.get("max_drawdown"), -1.0)
        days = int(challenger.get("forward_days", 0) or 0)
        trades = int(challenger.get("closed_trades", 0) or 0)
        champion_returns = champion.get("returns")
        challenger_returns = challenger.get("returns")
        boot = _paired_block_probability(
            champion_returns if champion_returns is not None else [],
            challenger_returns if challenger_returns is not None else [],
            str(arena["arena_id"]),
        )

        checks = {
            "enough_days": days >= min_days,
            "enough_closed_trades": trades >= min_trades,
            "positive_return": n_ret > min_return,
            "minimum_sharpe": n_sh >= min_sharpe,
            "drawdown_floor": n_dd >= min_mdd,
            "beats_champion_return": n_ret >= c_ret + return_edge,
            "beats_champion_sharpe": n_sh >= c_sh + sharpe_edge,
            "drawdown_not_materially_worse": n_dd >= c_dd - dd_slack,
            "paired_bootstrap_support": boot is not None and boot >= min_boot,
        }
        if all(checks.values()):
            verdict = "PROMOTE_READY"
        elif days >= max_days and trades >= min_trades:
            verdict = "REJECT_READY"
        elif not checks["enough_days"] or not checks["enough_closed_trades"]:
            verdict = "LEARNING"
        else:
            verdict = "CONTINUE"
        return {
            "verdict": verdict,
            "checks": checks,
            "bootstrap_probability": boot,
            "thresholds": {
                "min_forward_days": min_days,
                "min_closed_trades": min_trades,
                "max_forward_days": max_days,
                "min_return": min_return,
                "min_sharpe": min_sharpe,
                "min_max_drawdown": min_mdd,
                "return_edge": return_edge,
                "sharpe_edge": sharpe_edge,
                "drawdown_slack": dd_slack,
                "min_bootstrap_probability": min_boot,
            },
        }

    def _set_arena_result(self, arena: dict, status: str, verdict: str, gate: dict, now: str):
        decision_at = now if status != "ACTIVE" else None
        with self._c() as c:
            c.execute("""
              UPDATE arenas SET status=?,latest_verdict=?,latest_gate_json=?,
                decision_at=COALESCE(?,decision_at),updated_at=? WHERE arena_id=?
            """, (status, verdict, _json(gate), decision_at, now, arena["arena_id"]))

    def process_active(self, simulation_db, cache, now=None, progress=None):
        now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
        now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")
        now_iso = now_ts.isoformat()
        checked = updated = promoted = rejected = api_calls = 0
        errors = []

        arenas = self.arenas("ACTIVE")
        for arena in arenas:
            unit = f"{arena.get('market')}:{arena.get('symbol')}:{arena.get('horizon')}"
            notify_progress(progress, "GOVERNANCE", unit=unit, completed=checked, total=len(arenas))
            checked += 1
            try:
                market, symbol, horizon = arena["market"], arena["symbol"], arena["horizon"]
                pack = cache.ensure(market, symbol, horizon, now_ts)
                api_calls += int(bool(pack.get("api_called", False)))
                df = cache.closed_only(pack.get("data"), market, horizon, now_ts)
                if df is None or len(df) < 3:
                    continue
                champion_model = json.loads(arena["champion_model_json"])
                challenger_model = json.loads(arena["challenger_model_json"])
                champion = self._evaluate_model(arena, champion_model, df)
                challenger = self._evaluate_model(arena, challenger_model, df)
                if champion is None or challenger is None:
                    continue

                last_existing = self._latest_snapshot(arena["arena_id"], "CHALLENGER")
                if not last_existing or last_existing.get("last_forward_bar") != challenger["last_forward_bar"]:
                    self._save_snapshot(arena["arena_id"], "CHAMPION", champion, now_iso)
                    self._save_snapshot(arena["arena_id"], "CHALLENGER", challenger, now_iso)
                    updated += 1

                gate = self._gate(arena, champion, challenger)
                verdict = gate["verdict"]
                if verdict == "PROMOTE_READY":
                    promoted_model = dict(challenger_model)
                    promoted_model.update({
                        "market": market, "symbol": symbol, "horizon": horizon,
                        "updated_at": now_iso,
                    })
                    simulation_db.save_model(promoted_model)
                    self._set_arena_result(arena, "PROMOTED", "PROMOTED", gate, now_iso)
                    self.mark_research(market, symbol, horizon, arena["challenger_signature"], now_iso)
                    self._event(arena["arena_id"], market, symbol, horizon, "CHALLENGER_PROMOTED", gate, now_iso)
                    promoted += 1
                elif verdict == "REJECT_READY":
                    self._set_arena_result(arena, "REJECTED", "REJECTED", gate, now_iso)
                    self.mark_research(market, symbol, horizon, arena["challenger_signature"], now_iso)
                    self._event(arena["arena_id"], market, symbol, horizon, "CHALLENGER_REJECTED", gate, now_iso)
                    rejected += 1
                else:
                    self._set_arena_result(arena, "ACTIVE", verdict, gate, now_iso)
            except Exception as exc:
                errors.append({
                    "arena_id": arena.get("arena_id"), "market": arena.get("market"),
                    "symbol": arena.get("symbol"), "horizon": arena.get("horizon"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            finally:
                notify_progress(progress, "GOVERNANCE", unit=unit, completed=checked, total=len(arenas))

        return {
            "status": "OK" if not errors else "PARTIAL",
            "arenas_checked": checked,
            "snapshots_updated": updated,
            "promoted": promoted,
            "rejected": rejected,
            "market_data_api_calls": api_calls,
            "broker_order_api_calls": 0,
            "errors": errors,
        }

    def dashboard_rows(self, limit=200):
        rows = []
        for arena in self.arenas()[: int(limit)]:
            champion = self._latest_snapshot(arena["arena_id"], "CHAMPION") or {}
            challenger = self._latest_snapshot(arena["arena_id"], "CHALLENGER") or {}
            try:
                gate = json.loads(arena.get("latest_gate_json") or "{}")
            except Exception:
                gate = {}
            try:
                cm = json.loads(arena.get("champion_model_json") or "{}")
                nm = json.loads(arena.get("challenger_model_json") or "{}")
            except Exception:
                cm, nm = {}, {}
            failed = [k for k, v in (gate.get("checks") or {}).items() if not v]
            rows.append({
                "arena_id": arena["arena_id"],
                "market": arena["market"], "symbol": arena["symbol"], "horizon": arena["horizon"],
                "registered_at": arena["registered_at"], "status": arena["status"],
                "verdict": arena.get("latest_verdict"), "decision_at": arena.get("decision_at"),
                "champion_strategy": cm.get("strategy"), "challenger_strategy": nm.get("strategy"),
                "champion_params": cm.get("params"), "challenger_params": nm.get("params"),
                "forward_days": challenger.get("forward_days", 0),
                "champion_closed_trades": champion.get("closed_trades", 0),
                "challenger_closed_trades": challenger.get("closed_trades", 0),
                "champion_return": champion.get("total_return"),
                "challenger_return": challenger.get("total_return"),
                "champion_sharpe": champion.get("sharpe"),
                "challenger_sharpe": challenger.get("sharpe"),
                "champion_max_drawdown": champion.get("max_drawdown"),
                "challenger_max_drawdown": challenger.get("max_drawdown"),
                "bootstrap_probability": gate.get("bootstrap_probability"),
                "failed_checks": failed,
                "thresholds": gate.get("thresholds") or {},
            })
        return rows
