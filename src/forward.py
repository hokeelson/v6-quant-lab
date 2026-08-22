from __future__ import annotations
import ast
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from .data import AlpacaData, BinanceData, validate_ohlcv
from .strategies import trend_signal, momentum_signal, mean_reversion_signal, breakout_signal
from .backtest import ExecutionCosts, RiskRules
from .metrics import performance_metrics
from .forward_db import ForwardDB

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def candidate_id(market: str, symbol: str, strategy: str, params: dict, registered_at: str) -> str:
    frozen = json.dumps(
        {"market":market,"symbol":symbol,"strategy":strategy,"params":params,"registered_at":registered_at},
        ensure_ascii=False, sort_keys=True, separators=(",",":")
    )
    return hashlib.sha256(frozen.encode("utf-8")).hexdigest()[:20]

def signal_for(strategy: str, df: pd.DataFrame, params: dict) -> pd.Series:
    if strategy == "Trend MA":
        return trend_signal(df, **params)
    if strategy == "Momentum":
        return momentum_signal(df, **params)
    if strategy == "Mean Reversion RSI":
        return mean_reversion_signal(df, **params)
    if strategy == "Breakout":
        return breakout_signal(df, **params)
    raise KeyError(strategy)

@dataclass
class ForwardConfig:
    stock_costs: ExecutionCosts
    crypto_costs: ExecutionCosts
    risk: RiskRules
    stock_bars_per_year: int = 252
    crypto_bars_per_year: int = 365
    history_lookback_days: int = 500
    minimum_warmup_bars: int = 220

class ForwardManager:
    """
    Shadow forward validator.

    Invariants:
    - candidate parameters are frozen at registration.
    - bars with timestamp <= registered_at are NEVER counted as forward evidence.
    - signal on bar t may only change pending_target; execution occurs on a later bar open.
    - processing is idempotent via last_processed_bar and unique trade constraints.
    """
    def __init__(self, db: ForwardDB, config: ForwardConfig):
        self.db = db
        self.config = config

    def _market_settings(self, market: str):
        if market == "stock":
            return self.config.stock_costs, self.config.stock_bars_per_year
        if market == "crypto":
            return self.config.crypto_costs, self.config.crypto_bars_per_year
        raise ValueError(market)

    def _load_history(self, candidate: dict, end: pd.Timestamp | None = None) -> pd.DataFrame:
        end = end or pd.Timestamp.now(tz="UTC")
        start = end - pd.Timedelta(days=self.config.history_lookback_days)
        if candidate["market"] == "stock":
            return AlpacaData().bars(
                candidate["symbol"], start.isoformat(), end.isoformat(),
                timeframe="1Day", adjustment="all", feed="iex"
            )
        return BinanceData().bars(
            candidate["symbol"], start.isoformat(), end.isoformat(), interval="1d"
        )

    def process_candidate(self, candidate: dict, df: pd.DataFrame | None = None,
                          now_iso: str | None = None) -> dict:
        now_iso = now_iso or utc_now_iso()
        now_ts = pd.Timestamp(now_iso)
        if now_ts.tzinfo is None:
            now_ts = now_ts.tz_localize("UTC")
        else:
            now_ts = now_ts.tz_convert("UTC")

        df = self._load_history(candidate, now_ts) if df is None else df.copy()
        if df is None or len(df) < self.config.minimum_warmup_bars:
            return {"candidate_id":candidate["candidate_id"],"bars_processed":0,"reason":"insufficient_history"}

        v = validate_ohlcv(df)
        critical = sum(v[k] for k in [
            "duplicates","missing","bad_high","bad_low","nonpositive_price",
            "negative_volume","non_monotonic_time"
        ])
        if critical:
            return {"candidate_id":candidate["candidate_id"],"bars_processed":0,"reason":"invalid_ohlcv"}

        params = json.loads(candidate["params_json"]) if isinstance(candidate["params_json"], str) else candidate["params_json"]
        signal = signal_for(candidate["strategy"], df, params).reindex(df.index).fillna(0.0)

        state = self.db.state(candidate["candidate_id"])
        registered = pd.Timestamp(candidate["registered_at"])
        registered = registered.tz_localize("UTC") if registered.tzinfo is None else registered.tz_convert("UTC")

        last_processed = pd.Timestamp(state["last_processed_bar"]) if state.get("last_processed_bar") else None
        if last_processed is not None:
            last_processed = last_processed.tz_localize("UTC") if last_processed.tzinfo is None else last_processed.tz_convert("UTC")

        # Conservative closed-bar filter.
        # Crypto daily bar for the current UTC date is incomplete until the next UTC day.
        # For US stocks we only accept bars whose New York calendar date is before today.
        # This intentionally adds up to ~1 day of delay rather than risk using a partial daily bar.
        idx_utc = pd.DatetimeIndex(df.index)
        if idx_utc.tz is None:
            idx_utc = idx_utc.tz_localize("UTC")
        else:
            idx_utc = idx_utc.tz_convert("UTC")
        if candidate["market"] == "crypto":
            current_date = now_ts.floor("D").date()
            closed_mask = np.array([ts.date() < current_date for ts in idx_utc])
        else:
            now_ny_date = now_ts.tz_convert("America/New_York").date()
            closed_mask = np.array([ts.tz_convert("America/New_York").date() < now_ny_date for ts in idx_utc])
        closed_index = df.index[closed_mask]

        # Forward evidence must start strictly after registration and after last processed bar.
        eligible = closed_index[closed_index > registered]
        if last_processed is not None:
            eligible = eligible[eligible > last_processed]
        if len(eligible) == 0:
            return {"candidate_id":candidate["candidate_id"],"bars_processed":0,"reason":"no_new_closed_bar"}

        costs, _ = self._market_settings(candidate["market"])
        cost_rate = costs.one_way_rate
        risk = self.config.risk
        bars_processed = 0

        for ts in eligible:
            i = df.index.get_loc(ts)
            if not isinstance(i, (int, np.integer)) or i <= 0:
                continue
            o = float(df.loc[ts, "open"])
            h = float(df.loc[ts, "high"])
            l = float(df.loc[ts, "low"])
            c = float(df.loc[ts, "close"])

            cash = float(state["cash"])
            qty = float(state["qty"])
            entry_fill = state.get("entry_fill")
            entry_cost_basis = float(state.get("entry_cost_basis") or 0.0)
            pending = int(state.get("pending_target") or 0)
            prior_signal_bar = state.get("last_signal_bar")

            # Execute only a target that was created on an earlier bar.
            if pending == 1 and qty == 0:
                fill = o * (1 + cost_rate)
                allocation = min(cash, float(candidate["initial_capital"]) * risk.max_position_pct)
                new_qty = allocation / fill if fill > 0 else 0.0
                if new_qty > 0:
                    spent = new_qty * fill
                    cash -= spent
                    qty = new_qty
                    entry_fill = fill
                    entry_cost_basis = spent
                    self.db.insert_trade({
                        "candidate_id":candidate["candidate_id"],
                        "bar_time":ts.isoformat(),"action":"BUY","fill_price":fill,
                        "qty":qty,"realized_pnl":0.0,"reason":"SIGNAL",
                        "signal_bar":prior_signal_bar,"created_at":now_iso,
                    })
            elif pending == 0 and qty > 0:
                fill = o * (1 - cost_rate)
                proceeds = qty * fill
                pnl = proceeds - entry_cost_basis
                cash += proceeds
                self.db.insert_trade({
                    "candidate_id":candidate["candidate_id"],
                    "bar_time":ts.isoformat(),"action":"SELL","fill_price":fill,
                    "qty":qty,"realized_pnl":pnl,"reason":"SIGNAL",
                    "signal_bar":prior_signal_bar,"created_at":now_iso,
                })
                qty, entry_fill, entry_cost_basis = 0.0, None, 0.0

            # Intrabar protective exits, conservative same-bar tie: stop first.
            if qty > 0 and entry_fill is not None:
                stop = float(entry_fill) * (1 - risk.stop_loss_pct)
                target = float(entry_fill) * (1 + risk.take_profit_pct)
                exit_px = None
                reason = None
                if l <= stop:
                    exit_px, reason = stop * (1 - cost_rate), "STOP"
                elif h >= target:
                    exit_px, reason = target * (1 - cost_rate), "TAKE_PROFIT"
                if exit_px is not None:
                    proceeds = qty * exit_px
                    pnl = proceeds - entry_cost_basis
                    cash += proceeds
                    self.db.insert_trade({
                        "candidate_id":candidate["candidate_id"],
                        "bar_time":ts.isoformat(),"action":"SELL","fill_price":exit_px,
                        "qty":qty,"realized_pnl":pnl,"reason":reason,
                        "signal_bar":prior_signal_bar,"created_at":now_iso,
                    })
                    qty, entry_fill, entry_cost_basis = 0.0, None, 0.0

            equity = cash + qty * c
            self.db.upsert_equity({
                "candidate_id":candidate["candidate_id"],"bar_time":ts.isoformat(),
                "cash":cash,"qty":qty,"close_price":c,"equity":equity,
            })

            # Only now, after processing current bar's open/high/low/close, create
            # the target to be acted upon on a later bar.
            current_target = int(float(signal.loc[ts]) > 0)
            state = {
                "cash":cash,"qty":qty,"entry_fill":entry_fill,
                "entry_cost_basis":entry_cost_basis,"pending_target":current_target,
                "last_signal_bar":ts.isoformat(),
                "last_processed_bar":ts.isoformat(),
                "updated_at":now_iso,
            }
            self.db.upsert_state(candidate["candidate_id"], state)
            bars_processed += 1

        return {"candidate_id":candidate["candidate_id"],"bars_processed":bars_processed,"reason":"ok"}

    def run_once(self, data_override: dict[str,pd.DataFrame] | None = None, now_iso: str | None = None) -> dict:
        now_iso = now_iso or utc_now_iso()
        run_id = self.db.start_run(now_iso)
        checked = processed = 0
        errors = []
        try:
            for c in self.db.candidates("ACTIVE"):
                checked += 1
                try:
                    df = None if data_override is None else data_override.get(c["candidate_id"])
                    result = self.process_candidate(c, df=df, now_iso=now_iso)
                    processed += int(result.get("bars_processed",0))
                except Exception as e:
                    errors.append(f"{c['candidate_id']}: {type(e).__name__}: {e}")
            status = "OK" if not errors else "PARTIAL"
            self.db.finish_run(run_id, utc_now_iso(), status, checked, processed, "\n".join(errors) or None)
            return {"run_id":run_id,"status":status,"candidates_checked":checked,"bars_processed":processed,"errors":errors}
        except Exception as e:
            self.db.finish_run(run_id, utc_now_iso(), "FAILED", checked, processed, str(e))
            raise

def register_from_stage3(
    db: ForwardDB,
    deep_ranking: pd.DataFrame,
    market: str,
    initial_capital: float,
    top_n: int = 5,
    min_research_grade: float = 0.0,
    registered_at: str | None = None
) -> pd.DataFrame:
    registered_at = registered_at or utc_now_iso()
    rows = []
    x = deep_ranking.copy()
    if "research_grade" in x:
        x = x[x["research_grade"] >= float(min_research_grade)]
    x = x.head(int(top_n))
    for _, r in x.iterrows():
        params = r["best_params"]
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                try:
                    params = ast.literal_eval(params)
                except Exception:
                    params = {}
        cid = candidate_id(market, str(r["symbol"]), str(r["strategy"]), params, registered_at)
        item = {
            "candidate_id":cid,
            "market":market,
            "symbol":str(r["symbol"]),
            "strategy":str(r["strategy"]),
            "params":params,
            "registered_at":registered_at,
            "initial_capital":float(initial_capital),
            "research_grade":float(r["research_grade"]) if pd.notna(r.get("research_grade")) else None,
            "evidence_coverage":float(r["evidence_coverage"]) if pd.notna(r.get("evidence_coverage")) else None,
            "source_stage":"stage3",
            "status":"ACTIVE",
            "notes":"Frozen Stage 3 finalist. No pre-registration bars count as forward evidence.",
        }
        db.register_candidate(item)
        rows.append(item)
    return pd.DataFrame(rows)

def forward_scorecard(db: ForwardDB, candidate: dict, bars_per_year: int) -> dict:
    eq_rows = db.equity(candidate["candidate_id"])
    tr_rows = db.trades(candidate["candidate_id"])
    if not eq_rows:
        # A newly registered candidate may legitimately have zero forward bars.
        # Return a complete scorecard schema so ranking/sorting remains stable.
        return {
            "candidate_id": candidate["candidate_id"],
            "symbol": candidate["symbol"],
            "strategy": candidate["strategy"],
            "forward_bars": 0,
            "forward_days": 0,
            "closed_trades": 0,
            "total_return": 0.0,
            "sharpe": float("nan"),
            "max_drawdown": 0.0,
            "win_rate": float("nan"),
            "profit_factor": float("nan"),
            "forward_evidence": 0.0,
            "forward_score": 0.0,
            "research_grade": candidate.get("research_grade"),
            "status": candidate["status"],
        }
    eqdf = pd.DataFrame(eq_rows)
    eqdf["bar_time"] = pd.to_datetime(eqdf["bar_time"], utc=True)
    equity = pd.Series(eqdf["equity"].to_numpy(float), index=eqdf["bar_time"])
    tdf = pd.DataFrame(tr_rows)
    if len(tdf):
        tdf["timestamp"] = pd.to_datetime(tdf["bar_time"], utc=True)
        trades_for_metrics = tdf.rename(columns={"timestamp":"timestamp"})
    else:
        trades_for_metrics = pd.DataFrame()
    m = performance_metrics(equity, trades_for_metrics, bars_per_year, 0.0)
    first = eqdf["bar_time"].min()
    last = eqdf["bar_time"].max()
    days = max((last-first).days + 1, 1)
    closed = int(m.get("closed_trades",0))
    sh = float(np.nan_to_num(m.get("sharpe",np.nan), nan=-2.0, posinf=3.0, neginf=-3.0))
    mdd = abs(float(np.nan_to_num(m.get("max_drawdown",np.nan), nan=1.0)))
    ret = float(np.nan_to_num(m.get("total_return",np.nan), nan=-1.0))
    # Evidence grows gradually; early spectacular returns must not get a full score.
    time_evidence = min(days / 90.0, 1.0)
    trade_evidence = min(closed / 30.0, 1.0)
    evidence = 0.6*time_evidence + 0.4*trade_evidence
    quality = (
        45*np.tanh(sh/2.0)
        + 25*np.tanh(ret*3.0)
        + 20*(1-min(mdd,1))
        + 10
    )
    quality = float(np.clip(quality,0,100))
    score = quality * (0.35 + 0.65*evidence)
    return {
        "candidate_id":candidate["candidate_id"],"symbol":candidate["symbol"],
        "strategy":candidate["strategy"],"forward_bars":int(len(eqdf)),
        "forward_days":int(days),"closed_trades":closed,
        "total_return":m.get("total_return"),"sharpe":m.get("sharpe"),
        "max_drawdown":m.get("max_drawdown"),"win_rate":m.get("win_rate"),
        "profit_factor":m.get("profit_factor"),
        "forward_evidence":float(evidence),
        "forward_score":float(np.clip(score,0,100)),
        "research_grade":candidate.get("research_grade"),
        "status":candidate["status"],
    }

def rank_forward(db: ForwardDB, stock_bars_per_year: int = 252, crypto_bars_per_year: int = 365) -> pd.DataFrame:
    rows = []
    for c in db.candidates():
        bpy = stock_bars_per_year if c["market"] == "stock" else crypto_bars_per_year
        rows.append(forward_scorecard(db, c, bpy))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["forward_score","forward_evidence","sharpe"],
        ascending=False
    ).reset_index(drop=True)

def promotion_decision(score: dict) -> dict:
    """
    Conservative forward-only gate. This only decides research status, never live trading.
    """
    days = int(score.get("forward_days",0))
    trades = int(score.get("closed_trades",0))
    ret = score.get("total_return")
    sh = score.get("sharpe")
    mdd = score.get("max_drawdown")
    reasons = []
    if days < 60: reasons.append("forward_days<60")
    if trades < 20: reasons.append("closed_trades<20")
    if ret is None or not np.isfinite(ret) or ret <= 0: reasons.append("forward_return<=0")
    if sh is None or not np.isfinite(sh) or sh < 0.5: reasons.append("forward_sharpe<0.5")
    if mdd is None or not np.isfinite(mdd) or mdd < -0.25: reasons.append("max_drawdown<-25%")
    return {
        "eligible_for_extended_paper": len(reasons) == 0,
        "reasons": reasons,
        "note": "This gate only promotes to longer paper validation; it does not authorize real-money trading."
    }
