from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ENGINE_VERSION = "V10_ADAPTIVE_EVIDENCE_SHADOW"
EVALUATION_BARS = {"short": 6, "medium": 6, "long": 5}
ROUND_TRIP_COST_BPS = {"stock": 10.0, "crypto": 38.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS direction_predictions(
  prediction_key TEXT PRIMARY KEY,
  engine_version TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL,
  strategy TEXT,
  as_of TEXT NOT NULL,
  decision TEXT NOT NULL,
  confidence REAL,
  stop_distance REAL NOT NULL,
  target_distance REAL NOT NULL,
  evaluation_bars INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  evaluated_at TEXT,
  entry_bar TEXT,
  exit_bar TEXT,
  entry_price REAL,
  exit_price REAL,
  exit_reason TEXT,
  raw_return_pct REAL,
  directional_return_pct REAL,
  opportunity_return_pct REAL,
  reward_r REAL,
  hit INTEGER
);
CREATE INDEX IF NOT EXISTS idx_direction_pending
ON direction_predictions(status,market,symbol,horizon,as_of);
CREATE INDEX IF NOT EXISTS idx_direction_evaluated
ON direction_predictions(engine_version,decision,evaluated_at DESC);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value, default=0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _iso(value) -> str:
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.isoformat()


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def _max_drawdown(returns: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _payload(row: dict) -> dict:
    try:
        value = json.loads(row.get("payload_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _clamped_confidence(value) -> float:
    return min(1.0, max(0.0, _finite(value)))


def _calibration(rows: list[dict]) -> dict:
    """Reliability diagnostics; never re-tunes the Shadow policy."""
    trades = [row for row in rows if row.get("decision") in ("LONG", "SHORT")]
    if not trades:
        return {"samples": 0, "brier_score": None, "mean_confidence": None, "observed_hit_rate": None, "bins": []}
    bins: dict[str, list[dict]] = {}
    errors = []
    for row in trades:
        confidence = _clamped_confidence(row.get("confidence"))
        hit = int(row.get("hit") or 0)
        errors.append((confidence - hit) ** 2)
        lower = min(0.9, math.floor(confidence * 10.0) / 10.0)
        label = f"{lower:.1f}-{lower + 0.1:.1f}"
        bins.setdefault(label, []).append({"confidence": confidence, "hit": hit})
    return {
        "samples": len(trades),
        "brier_score": _mean(errors),
        "mean_confidence": _mean([_clamped_confidence(row.get("confidence")) for row in trades]),
        "observed_hit_rate": _mean([float(int(row.get("hit") or 0)) for row in trades]),
        "bins": [
            {
                "range": label,
                "samples": len(group),
                "mean_confidence": _mean([item["confidence"] for item in group]),
                "observed_hit_rate": _mean([float(item["hit"]) for item in group]),
            }
            for label, group in sorted(bins.items())
        ],
    }


def _slice_diagnostics(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for row in rows:
        payload = _payload(row)
        key = (
            str(row.get("market") or "UNKNOWN"),
            str(row.get("horizon") or "UNKNOWN"),
            str(row.get("decision") or "UNKNOWN"),
            str(payload.get("regime") or "UNKNOWN"),
        )
        groups.setdefault(key, []).append(row)
    output = []
    for (market, horizon, decision, regime), group in groups.items():
        returns = [_finite(row.get("directional_return_pct")) for row in group]
        output.append({
            "market": market,
            "horizon": horizon,
            "decision": decision,
            "regime": regime,
            "samples": len(group),
            "avg_forward_return_pct": _mean(returns),
            "hit_rate": _mean([float(int(row.get("hit") or 0)) for row in group]),
            "maturity": "DIAGNOSTIC" if len(group) < 20 else "PRELIMINARY" if len(group) < 50 else "CREDIBLE",
        })
    return sorted(output, key=lambda item: (item["avg_forward_return_pct"], -item["samples"]))


class DirectionForwardLedger:
    """Isolated, non-overlapping Forward ledger for direction Shadow decisions."""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _key(self, row: dict) -> str:
        raw = ":".join([
            str(row.get("engine_version") or ENGINE_VERSION),
            str(row.get("market") or ""),
            str(row.get("symbol") or "").upper(),
            str(row.get("horizon") or ""),
            _iso(row.get("as_of")),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def has_pending(self, market: str, symbol: str, horizon: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                """SELECT 1 FROM direction_predictions
                   WHERE status='PENDING' AND market=? AND symbol=? AND horizon=? LIMIT 1""",
                (str(market), str(symbol).upper(), str(horizon)),
            ).fetchone()
        return row is not None

    def register(self, row: dict) -> dict:
        market = str(row.get("market") or "")
        symbol = str(row.get("symbol") or "").upper()
        horizon = str(row.get("horizon") or "")
        decision = str(row.get("direction") or "NO_TRADE")
        if market not in ("stock", "crypto") or not symbol or horizon not in EVALUATION_BARS:
            return {"registered": False, "reason": "INVALID_SCOPE"}
        if decision not in ("LONG", "SHORT", "NO_TRADE"):
            return {"registered": False, "reason": "INVALID_DECISION"}
        if self.has_pending(market, symbol, horizon):
            return {"registered": False, "reason": "PENDING_EXISTS"}

        clean = {
            "adaptive_weights": row.get("adaptive_weights") or {},
            "evidence_contributions": row.get("evidence_contributions") or {},
            "decision_reasons": row.get("decision_reasons") or [],
            "preferred_playbook": row.get("preferred_playbook"),
            "stability_score": _finite(row.get("stability_score")),
            "evidence_agreement": _finite(row.get("evidence_agreement")),
            "evidence_coverage": _finite(row.get("evidence_coverage")),
            "volume_edge": _finite(row.get("volume_edge")),
            "external_edge": _finite(row.get("external_edge")),
            "trend_edge": _finite(row.get("trend_edge")),
            "regime": row.get("regime"),
        }
        prediction = {
            **row,
            "engine_version": str(row.get("engine_version") or ENGINE_VERSION),
            "market": market,
            "symbol": symbol,
            "horizon": horizon,
            "as_of": _iso(row.get("as_of")),
        }
        key = self._key(prediction)
        with self._connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO direction_predictions(
                     prediction_key,engine_version,market,symbol,horizon,strategy,as_of,decision,
                     confidence,stop_distance,target_distance,evaluation_bars,payload_json,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?)""",
                (
                    key, prediction["engine_version"], market, symbol, horizon,
                    str(row.get("strategy") or ""), prediction["as_of"], decision,
                    _finite(row.get("direction_confidence")),
                    max(0.005, _finite(row.get("stop_distance"), 0.03)),
                    max(0.005, _finite(row.get("target_distance"), 0.06)),
                    int(EVALUATION_BARS[horizon]), _json(clean), _now_iso(),
                ),
            )
        return {"registered": bool(cur.rowcount), "reason": "REGISTERED" if cur.rowcount else "DUPLICATE", "prediction_key": key}

    def _pending(self, market: str, symbol: str, horizon: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT * FROM direction_predictions
                   WHERE status='PENDING' AND market=? AND symbol=? AND horizon=? ORDER BY as_of""",
                (str(market), str(symbol).upper(), str(horizon)),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _trade_path(window: pd.DataFrame, decision: str, entry: float, stop: float, target: float) -> tuple[float, str, str]:
        stop_price = entry * (1.0 - stop) if decision == "LONG" else entry * (1.0 + stop)
        target_price = entry * (1.0 + target) if decision == "LONG" else entry * (1.0 - target)
        for index, bar in window.iterrows():
            low, high = _finite(bar.get("low"), entry), _finite(bar.get("high"), entry)
            if decision == "LONG":
                # Same-bar stop+target is resolved against the strategy.
                if low <= stop_price:
                    return stop_price, "STOP", _iso(index)
                if high >= target_price:
                    return target_price, "TARGET", _iso(index)
            else:
                if high >= stop_price:
                    return stop_price, "STOP", _iso(index)
                if low <= target_price:
                    return target_price, "TARGET", _iso(index)
        last = window.iloc[-1]
        return _finite(last.get("close"), entry), "TIME", _iso(window.index[-1])

    def evaluate_pair(self, df: pd.DataFrame, market: str, symbol: str, horizon: str) -> dict:
        if df is None or df.empty:
            return {"evaluated": 0, "waiting": len(self._pending(market, symbol, horizon))}
        data = df.copy()
        data.index = pd.to_datetime(data.index, utc=True)
        data = data.sort_index()
        evaluated = waiting = 0
        for prediction in self._pending(market, symbol, horizon):
            after = data[data.index > pd.Timestamp(prediction["as_of"])]
            bars = int(prediction["evaluation_bars"])
            if len(after) < bars:
                waiting += 1
                continue
            window = after.iloc[:bars]
            first = window.iloc[0]
            entry = _finite(first.get("open"), _finite(first.get("close")))
            if entry <= 0:
                waiting += 1
                continue
            decision = str(prediction["decision"])
            stop = _finite(prediction["stop_distance"], 0.03)
            target = max(stop, _finite(prediction["target_distance"], stop * 1.5))
            cost = ROUND_TRIP_COST_BPS.get(str(market), 20.0) / 10000.0

            if decision in ("LONG", "SHORT"):
                exit_price, exit_reason, exit_bar = self._trade_path(window, decision, entry, stop, target)
                raw_return = exit_price / entry - 1.0
                directional = (raw_return if decision == "LONG" else -raw_return) - cost
                opportunity = max(raw_return - cost, -raw_return - cost)
                reward_r = directional / max(stop, 1e-12)
                hit = int(directional > 0.0)
            else:
                exit_price = _finite(window.iloc[-1].get("close"), entry)
                exit_reason, exit_bar = "WAIT_WINDOW", _iso(window.index[-1])
                raw_return = exit_price / entry - 1.0
                directional = 0.0
                opportunity = max(raw_return - cost, -raw_return - cost, 0.0)
                no_trade_threshold = max(cost, min(0.015, stop * 0.25))
                reward_r = -opportunity / max(stop, 1e-12)
                hit = int(opportunity <= no_trade_threshold)

            with self._connect() as con:
                con.execute(
                    """UPDATE direction_predictions SET
                         status='EVALUATED',evaluated_at=?,entry_bar=?,exit_bar=?,entry_price=?,exit_price=?,
                         exit_reason=?,raw_return_pct=?,directional_return_pct=?,opportunity_return_pct=?,reward_r=?,hit=?
                       WHERE prediction_key=? AND status='PENDING'""",
                    (
                        _now_iso(), _iso(window.index[0]), exit_bar, entry, exit_price, exit_reason,
                        raw_return, directional, opportunity, reward_r, hit, prediction["prediction_key"],
                    ),
                )
            evaluated += 1
        return {"evaluated": evaluated, "waiting": waiting}

    def summary(self) -> dict:
        decision_stats = {}
        with self._connect() as con:
            pending = int(con.execute("SELECT COUNT(*) FROM direction_predictions WHERE status='PENDING'").fetchone()[0])
            evaluated = int(con.execute("SELECT COUNT(*) FROM direction_predictions WHERE status='EVALUATED'").fetchone()[0])
            rows = con.execute(
                """SELECT decision,COUNT(*) AS completed,AVG(directional_return_pct) AS avg_return,
                          AVG(opportunity_return_pct) AS avg_opportunity,AVG(reward_r) AS avg_reward_r,
                          AVG(hit) AS hit_rate
                   FROM direction_predictions WHERE status='EVALUATED' GROUP BY decision"""
            ).fetchall()
            policies = con.execute(
                """SELECT engine_version,COUNT(*) AS predictions,
                          SUM(CASE WHEN status='EVALUATED' THEN 1 ELSE 0 END) AS evaluated
                   FROM direction_predictions GROUP BY engine_version ORDER BY engine_version"""
            ).fetchall()
            evaluated_rows = [dict(row) for row in con.execute(
                """SELECT * FROM direction_predictions
                   WHERE status='EVALUATED' ORDER BY as_of,prediction_key"""
            ).fetchall()]
        for row in rows:
            decision_stats[str(row["decision"])] = {
                "evaluated": int(row["completed"] or 0),
                "completed": int(row["completed"] or 0),
                "avg_forward_return_pct": _finite(row["avg_return"]),
                "avg_opportunity_return_pct": _finite(row["avg_opportunity"]),
                "avg_reward_r": _finite(row["avg_reward_r"]),
                "hit_rate": _finite(row["hit_rate"]),
            }
        trade_rows = [row for row in evaluated_rows if row.get("decision") in ("LONG", "SHORT")]
        policy_returns = [_finite(row.get("directional_return_pct")) for row in evaluated_rows]
        trade_returns = [_finite(row.get("directional_return_pct")) for row in trade_rows]
        always_long = [
            _finite(row.get("raw_return_pct")) - ROUND_TRIP_COST_BPS.get(str(row.get("market")), 20.0) / 10000.0
            for row in evaluated_rows
        ]
        span_days = 0.0
        if len(evaluated_rows) >= 2:
            first = pd.Timestamp(evaluated_rows[0]["as_of"])
            last = pd.Timestamp(evaluated_rows[-1]["as_of"])
            span_days = max(0.0, (last - first).total_seconds() / 86400.0)
        trade_std = _sample_std(trade_returns)
        trade_sharpe = _mean(trade_returns) / trade_std if trade_std > 0 else 0.0
        preliminary = len(trade_rows) >= 20
        credible = len(trade_rows) >= 50 and span_days >= 60.0
        promotion_checks = {
            "minimum_60_forward_days": span_days >= 60.0,
            "minimum_20_closed_trades": len(trade_rows) >= 20,
            "positive_average_return": _mean(trade_returns) > 0.0,
            "trade_sequence_sharpe_at_least_0_5": trade_sharpe >= 0.5,
            "max_drawdown_no_worse_than_minus_25pct": _max_drawdown(policy_returns) >= -0.25,
        }
        extended_paper_candidate = all(promotion_checks.values())
        return {
            "engine_version": ENGINE_VERSION,
            "pending": pending,
            "evaluated": evaluated,
            "decision_stats": decision_stats,
            "policies": [dict(row) for row in policies],
            "evidence_maturity": {
                "forward_days": span_days,
                "directional_trades": len(trade_rows),
                "state": "CREDIBLE" if credible else "PRELIMINARY" if preliminary else "LEARNING",
                "automatic_retuning_allowed": False,
                "reason": "Forward evidence is diagnostic until independently reviewed; no online parameter chasing.",
            },
            "policy_performance": {
                "samples_including_no_trade": len(policy_returns),
                "directional_trade_samples": len(trade_returns),
                "avg_policy_return_pct": _mean(policy_returns),
                "avg_directional_trade_return_pct": _mean(trade_returns),
                "trade_sequence_sharpe": trade_sharpe,
                "max_drawdown_pct": _max_drawdown(policy_returns),
            },
            "benchmarks": {
                "always_long_after_cost_avg_return_pct": _mean(always_long),
                "policy_minus_always_long_avg_return_pct": _mean(policy_returns) - _mean(always_long),
                "note": "Equal-weight prediction-window diagnostic; not a portfolio equity curve.",
            },
            "confidence_calibration": _calibration(evaluated_rows),
            "slice_diagnostics": _slice_diagnostics(evaluated_rows),
            "promotion_gate": {
                "decision": "EXTENDED_PAPER_CANDIDATE" if extended_paper_candidate else "HOLD_SHADOW",
                "checks": promotion_checks,
                "real_money_authorized": False,
            },
            "shadow_only": True,
            "broker_order_api_calls": 0,
        }

    def recent(self, limit: int = 200) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM direction_predictions ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]
