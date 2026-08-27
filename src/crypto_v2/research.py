from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Mapping

import numpy as np
import pandas as pd


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def session_label(ts) -> str:
    """Coarse UTC liquidity session label for research only."""
    hour = int(_utc(ts).hour)
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 13:
        return "EUROPE"
    if 13 <= hour < 17:
        return "EU_US_OVERLAP"
    return "US"


def _through(df: pd.DataFrame, cutoff) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    idx = pd.to_datetime(df.index, utc=True, errors="coerce")
    valid = ~pd.isna(idx)
    if not bool(np.any(valid)):
        return pd.DataFrame()
    positions = np.flatnonzero(np.asarray(valid, dtype=bool))
    valid_idx = pd.DatetimeIndex(idx[positions])
    mask = np.asarray(valid_idx.asi8 <= _utc(cutoff).value, dtype=bool)
    return df.iloc[positions[np.flatnonzero(mask)]].copy()


def market_context(ts, horizon: str, frames: Mapping[tuple[str, str], pd.DataFrame], btc_1h: pd.DataFrame) -> dict:
    """Cross-sectional telemetry from the already-populated OHLCV cache only.

    These fields are descriptive research data. They are never read by the V2
    router, execution logic, or portfolio risk governor.
    """
    cutoff = _utc(ts)
    btc = _through(btc_1h, cutoff)
    btc_ret_24h = None
    if len(btc) >= 25:
        prev = float(btc.close.iloc[-25])
        btc_ret_24h = float(btc.close.iloc[-1] / prev - 1.0) if prev else None

    above_ema20 = []
    one_bar_returns = []
    return_series = []
    symbols_used = []

    for (symbol, h), df in frames.items():
        if h != horizon:
            continue
        hist = _through(df, cutoff)
        if len(hist) < 3:
            continue
        close = pd.to_numeric(hist.close, errors="coerce").dropna()
        if len(close) < 3:
            continue
        symbols_used.append(str(symbol))
        one_bar_returns.append(float(close.iloc[-1] / close.iloc[-2] - 1.0))
        if len(close) >= 20:
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            above_ema20.append(float(close.iloc[-1]) > ema20)
        rets = close.pct_change().replace([np.inf, -np.inf], np.nan).dropna().tail(24)
        if len(rets) >= 6:
            rets.name = str(symbol)
            return_series.append(rets)

    avg_corr = None
    if len(return_series) >= 3:
        aligned = pd.concat(return_series[:40], axis=1, join="inner").dropna(how="any")
        if len(aligned) >= 6 and aligned.shape[1] >= 3:
            corr = aligned.corr().to_numpy(dtype=float)
            tri = corr[np.triu_indices_from(corr, k=1)]
            finite = tri[np.isfinite(tri)]
            if finite.size:
                avg_corr = float(np.mean(finite))

    return {
        "version": 1,
        "session": session_label(ts),
        "horizon": str(horizon),
        "breadth_above_ema20": (float(np.mean(above_ema20)) if above_ema20 else None),
        "median_one_bar_return": (float(np.median(one_bar_returns)) if one_bar_returns else None),
        "avg_pairwise_correlation": avg_corr,
        "btc_return_24h": btc_ret_24h,
        "symbols_observed": len(symbols_used),
        "funding_rate": None,
        "open_interest_change": None,
        "liquidation_shock_score": None,
        "external_derivatives_data": "NOT_CONNECTED",
    }


def ensure_research_schema(db) -> None:
    """Create research-only tables alongside, but isolated from, the V2 ledger."""
    with db._c() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS research_positions(
          symbol TEXT NOT NULL,
          horizon TEXT NOT NULL,
          entry_bar TEXT NOT NULL,
          entry_price REAL NOT NULL,
          entry_session TEXT NOT NULL,
          strategy TEXT NOT NULL,
          regime_entry TEXT NOT NULL,
          mfe_pct REAL NOT NULL DEFAULT 0,
          mae_pct REAL NOT NULL DEFAULT 0,
          context_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(symbol,horizon)
        );
        CREATE TABLE IF NOT EXISTS research_trades(
          trade_id TEXT PRIMARY KEY,
          symbol TEXT NOT NULL,
          horizon TEXT NOT NULL,
          entry_session TEXT NOT NULL,
          mfe_pct REAL NOT NULL,
          mae_pct REAL NOT NULL,
          context_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blocked_candidates(
          candidate_id TEXT PRIMARY KEY,
          symbol TEXT NOT NULL,
          horizon TEXT NOT NULL,
          decision_bar TEXT NOT NULL,
          requested_notional REAL NOT NULL,
          strategy TEXT NOT NULL,
          regime_entry TEXT NOT NULL,
          stop_distance REAL NOT NULL,
          target_distance REAL NOT NULL,
          max_holding_bars INTEGER NOT NULL,
          entry_session TEXT NOT NULL,
          context_json TEXT NOT NULL,
          status TEXT NOT NULL,
          entry_bar TEXT,
          entry_price REAL,
          stop_price REAL,
          target_price REAL,
          bars_held INTEGER NOT NULL DEFAULT 0,
          mfe_pct REAL NOT NULL DEFAULT 0,
          mae_pct REAL NOT NULL DEFAULT 0,
          exit_bar TEXT,
          exit_price REAL,
          return_pct REAL,
          simulated_pnl REAL,
          exit_reason TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(symbol,horizon,decision_bar)
        );
        """)


def record_position_open(db, symbol: str, horizon: str, entry_bar: str, entry_price: float,
                         strategy: str, regime: str, context: dict | None) -> None:
    ensure_research_schema(db)
    ctx = dict(context or {})
    with db._c() as c:
        c.execute(
            """INSERT OR REPLACE INTO research_positions(
                 symbol,horizon,entry_bar,entry_price,entry_session,strategy,regime_entry,
                 mfe_pct,mae_pct,context_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                symbol.upper(), horizon, entry_bar, float(entry_price),
                str(ctx.get("session") or session_label(entry_bar)), str(strategy or "UNKNOWN"),
                str(regime or "UNKNOWN"), 0.0, 0.0,
                json.dumps(ctx, ensure_ascii=False, sort_keys=True), now_iso(),
            ),
        )


def update_position_excursion(db, symbol: str, horizon: str, high: float, low: float) -> None:
    ensure_research_schema(db)
    with db._c() as c:
        row = c.execute(
            "SELECT * FROM research_positions WHERE symbol=? AND horizon=?",
            (symbol.upper(), horizon),
        ).fetchone()
        if not row:
            return
        entry = float(row["entry_price"])
        if entry <= 0:
            return
        mfe = max(float(row["mfe_pct"]), float(high) / entry - 1.0)
        mae = min(float(row["mae_pct"]), float(low) / entry - 1.0)
        c.execute(
            "UPDATE research_positions SET mfe_pct=?,mae_pct=? WHERE symbol=? AND horizon=?",
            (mfe, mae, symbol.upper(), horizon),
        )


def record_position_close(db, symbol: str, horizon: str, exit_bar: str) -> None:
    ensure_research_schema(db)
    with db._c() as c:
        rp = c.execute(
            "SELECT * FROM research_positions WHERE symbol=? AND horizon=?",
            (symbol.upper(), horizon),
        ).fetchone()
        trade = c.execute(
            """SELECT trade_id FROM trades
               WHERE symbol=? AND horizon=? AND exit_bar=?
               ORDER BY created_at DESC LIMIT 1""",
            (symbol.upper(), horizon, exit_bar),
        ).fetchone()
        if not rp or not trade:
            return
        c.execute(
            """INSERT OR REPLACE INTO research_trades(
                 trade_id,symbol,horizon,entry_session,mfe_pct,mae_pct,context_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                trade["trade_id"], symbol.upper(), horizon, rp["entry_session"],
                float(rp["mfe_pct"]), float(rp["mae_pct"]), rp["context_json"], now_iso(),
            ),
        )
        c.execute("DELETE FROM research_positions WHERE symbol=? AND horizon=?", (symbol.upper(), horizon))


def add_blocked_candidate(db, symbol: str, horizon: str, decision_bar: str, requested_notional: float,
                          decision: dict, regime: dict, context: dict | None) -> None:
    """Track a risk-blocked entry counterfactually without touching cash or positions."""
    ensure_research_schema(db)
    ctx = dict(context or {})
    with db._c() as c:
        active = c.execute(
            """SELECT 1 FROM blocked_candidates
               WHERE symbol=? AND horizon=? AND status IN ('PENDING_ENTRY','OPEN') LIMIT 1""",
            (symbol.upper(), horizon),
        ).fetchone()
        if active:
            return
        c.execute(
            """INSERT OR IGNORE INTO blocked_candidates(
                 candidate_id,symbol,horizon,decision_bar,requested_notional,strategy,regime_entry,
                 stop_distance,target_distance,max_holding_bars,entry_session,context_json,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex, symbol.upper(), horizon, decision_bar, float(requested_notional),
                str(decision.get("strategy") or "UNKNOWN"), str(regime.get("state") or "UNKNOWN"),
                float(decision.get("stop_distance") or 0.03), float(decision.get("target_distance") or 0.06),
                int(decision.get("max_holding_bars") or 8), str(ctx.get("session") or session_label(decision_bar)),
                json.dumps(ctx, ensure_ascii=False, sort_keys=True), "PENDING_ENTRY", now_iso(),
            ),
        )


def manage_blocked_candidate(db, symbol: str, horizon: str, bar_time: str, row, fee_rate: float) -> None:
    """Advance one independent counterfactual candidate on the current closed bar."""
    ensure_research_schema(db)
    with db._c() as c:
        r = c.execute(
            """SELECT * FROM blocked_candidates
               WHERE symbol=? AND horizon=? AND status IN ('PENDING_ENTRY','OPEN')
               ORDER BY created_at LIMIT 1""",
            (symbol.upper(), horizon),
        ).fetchone()
        if not r:
            return
        d = dict(r)
        if d["status"] == "PENDING_ENTRY":
            if _utc(d["decision_bar"]) >= _utc(bar_time):
                return
            entry = float(row.open)
            if entry <= 0:
                return
            stop = entry * (1.0 - float(d["stop_distance"]))
            target = entry * (1.0 + float(d["target_distance"]))
            c.execute(
                """UPDATE blocked_candidates
                   SET status='OPEN',entry_bar=?,entry_price=?,stop_price=?,target_price=?
                   WHERE candidate_id=?""",
                (bar_time, entry, stop, target, d["candidate_id"]),
            )
            d.update({"status": "OPEN", "entry_bar": bar_time, "entry_price": entry, "stop_price": stop, "target_price": target})

        entry = float(d["entry_price"])
        high = float(row.high)
        low = float(row.low)
        open_px = float(row.open)
        close_px = float(row.close)
        mfe = max(float(d.get("mfe_pct") or 0.0), high / entry - 1.0)
        mae = min(float(d.get("mae_pct") or 0.0), low / entry - 1.0)
        stop = float(d["stop_price"])
        target = float(d["target_price"])
        reason = None
        exit_px = None
        bars_held = int(d.get("bars_held") or 0)

        if low <= stop:
            exit_px = min(open_px, stop) if open_px < stop else stop
            reason = "STOP"
        elif high >= target:
            exit_px = target
            reason = "TARGET"
        else:
            bars_held += 1
            if bars_held >= int(d["max_holding_bars"]):
                exit_px = close_px
                reason = "TIME"

        if reason is None:
            c.execute(
                "UPDATE blocked_candidates SET bars_held=?,mfe_pct=?,mae_pct=? WHERE candidate_id=?",
                (bars_held, mfe, mae, d["candidate_id"]),
            )
            return

        gross_entry = float(d["requested_notional"])
        qty = gross_entry / entry if entry > 0 else 0.0
        entry_cost = gross_entry * (1.0 + fee_rate)
        proceeds = qty * float(exit_px) * (1.0 - fee_rate)
        pnl = proceeds - entry_cost
        ret = pnl / entry_cost if entry_cost > 0 else 0.0
        c.execute(
            """UPDATE blocked_candidates
               SET status='CLOSED',bars_held=?,mfe_pct=?,mae_pct=?,exit_bar=?,exit_price=?,
                   return_pct=?,simulated_pnl=?,exit_reason=? WHERE candidate_id=?""",
            (bars_held, mfe, mae, bar_time, float(exit_px), ret, pnl, reason, d["candidate_id"]),
        )


def _joined_research_trades(db, limit: int = 500) -> list[dict]:
    ensure_research_schema(db)
    with db._c() as c:
        rows = c.execute(
            """SELECT t.*,r.entry_session,r.mfe_pct,r.mae_pct,r.context_json
               FROM trades t JOIN research_trades r ON r.trade_id=t.trade_id
               ORDER BY t.exit_bar DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def research_summary(db) -> dict:
    trades = _joined_research_trades(db, 5000)
    by_session: dict[str, dict] = {}
    for t in trades:
        session = str(t.get("entry_session") or "UNKNOWN")
        bucket = by_session.setdefault(session, {"closed_trades": 0, "wins": 0, "realized_pnl": 0.0, "return_sum": 0.0})
        bucket["closed_trades"] += 1
        pnl = float(t.get("realized_pnl") or 0.0)
        ret = float(t.get("return_pct") or 0.0)
        bucket["wins"] += int(pnl > 0)
        bucket["realized_pnl"] += pnl
        bucket["return_sum"] += ret
    for bucket in by_session.values():
        n = int(bucket["closed_trades"])
        bucket["win_rate"] = bucket["wins"] / n if n else None
        bucket["avg_return_pct"] = bucket.pop("return_sum") / n if n else None

    with db._c() as c:
        blocked = [dict(r) for r in c.execute(
            "SELECT * FROM blocked_candidates WHERE status='CLOSED' ORDER BY exit_bar DESC LIMIT 5000"
        ).fetchall()]
        active_blocked = int(c.execute(
            "SELECT COUNT(*) FROM blocked_candidates WHERE status IN ('PENDING_ENTRY','OPEN')"
        ).fetchone()[0])

    avoided_losses = sum(1 for r in blocked if float(r.get("return_pct") or 0.0) < 0)
    missed_winners = sum(1 for r in blocked if float(r.get("return_pct") or 0.0) > 0)
    return {
        "version": 1,
        "trade_excursion_tracking": {
            "tracked_closed_trades": len(trades),
            "avg_mfe_pct": (float(np.mean([float(t["mfe_pct"]) for t in trades])) if trades else None),
            "avg_mae_pct": (float(np.mean([float(t["mae_pct"]) for t in trades])) if trades else None),
            "by_session": by_session,
        },
        "risk_block_counterfactual": {
            "closed_candidates": len(blocked),
            "active_candidates": active_blocked,
            "avoided_losses": avoided_losses,
            "missed_winners": missed_winners,
            "avoided_loss_rate": avoided_losses / len(blocked) if blocked else None,
            "counterfactual_pnl": float(sum(float(r.get("simulated_pnl") or 0.0) for r in blocked)),
        },
        "external_signals": {
            "funding_rate": "NOT_CONNECTED",
            "open_interest": "NOT_CONNECTED",
            "liquidations": "NOT_CONNECTED",
            "policy": "OBSERVE_FIRST_NO_STRATEGY_INPUT",
        },
    }


def recent_research_trades(db, limit: int = 100) -> list[dict]:
    return _joined_research_trades(db, limit)


def recent_blocked_candidates(db, limit: int = 100) -> list[dict]:
    ensure_research_schema(db)
    with db._c() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM blocked_candidates ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()]
