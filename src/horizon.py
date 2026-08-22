from __future__ import annotations
import ast
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from .backtest import ExecutionCosts, RiskRules
from .data import AlpacaData, BinanceData, validate_ohlcv
from .forward_db import ForwardDB
from .horizon_db import HorizonDB
from .metrics import performance_metrics
from .strategies import trend_signal, momentum_signal, mean_reversion_signal, breakout_signal

HORIZON_ORDER = ["short", "medium", "long"]
HORIZON_LABELS = {
    "short": "短線（數天）",
    "medium": "中線（數週）",
    "long": "長線（數月）",
}

HORIZON_PROFILES = {
    "short": {
        "max_position_pct": 0.10,
        "stop_loss_pct": 0.04,
        "take_profit_pct": 0.08,
        "max_holding_bars": 10,
        "evidence_days": 60,
        "evidence_trades": 20,
    },
    "medium": {
        "max_position_pct": 0.15,
        "stop_loss_pct": 0.08,
        "take_profit_pct": 0.20,
        "max_holding_bars": 40,
        "evidence_days": 120,
        "evidence_trades": 12,
    },
    "long": {
        "max_position_pct": 0.20,
        "stop_loss_pct": 0.15,
        "take_profit_pct": 0.40,
        "max_holding_bars": 180,
        "evidence_days": 240,
        "evidence_trades": 6,
    },
}

PRESETS = {
    "short": {
        "Trend MA": {"fast":5,"slow":20},
        "Momentum": {"lookback":10,"threshold":0.02},
        "Mean Reversion RSI": {"rsi_n":7,"entry":30,"exit_":55},
        "Breakout": {"lookback":10,"exit_lookback":5},
    },
    "long": {
        "Trend MA": {"fast":50,"slow":200},
        "Momentum": {"lookback":120,"threshold":0.06},
        "Mean Reversion RSI": {"rsi_n":21,"entry":30,"exit_":60},
        "Breakout": {"lookback":120,"exit_lookback":40},
    },
}

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_params(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            try:
                v = ast.literal_eval(value)
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}
    return {}

def params_for_horizon(strategy: str, horizon: str, base_params: dict) -> dict:
    if horizon == "medium":
        return dict(base_params)
    if horizon not in PRESETS or strategy not in PRESETS[horizon]:
        raise KeyError(f"Unsupported horizon/strategy: {horizon}/{strategy}")
    return dict(PRESETS[horizon][strategy])

def sleeve_id(base_candidate_id: str, horizon: str, params: dict) -> str:
    frozen = json.dumps(
        {"base_candidate_id":base_candidate_id,"horizon":horizon,"params":params},
        sort_keys=True,separators=(",",":"),ensure_ascii=False,
    )
    return hashlib.sha256(frozen.encode("utf-8")).hexdigest()[:20]

def signal_for(strategy: str, df: pd.DataFrame, params: dict) -> pd.Series:
    if strategy == "Trend MA": return trend_signal(df, **params)
    if strategy == "Momentum": return momentum_signal(df, **params)
    if strategy == "Mean Reversion RSI": return mean_reversion_signal(df, **params)
    if strategy == "Breakout": return breakout_signal(df, **params)
    raise KeyError(strategy)

@dataclass
class HorizonConfig:
    stock_costs: ExecutionCosts
    crypto_costs: ExecutionCosts
    stock_bars_per_year: int = 252
    crypto_bars_per_year: int = 365
    history_lookback_days: int = 1000
    minimum_warmup_bars: int = 220

class HorizonManager:
    def __init__(self, db: HorizonDB, config: HorizonConfig):
        self.db = db
        self.config = config

    def _settings(self, market: str, horizon: str):
        costs = self.config.stock_costs if market == "stock" else self.config.crypto_costs
        bars_per_year = self.config.stock_bars_per_year if market == "stock" else self.config.crypto_bars_per_year
        p = HORIZON_PROFILES[horizon]
        risk = RiskRules(
            max_position_pct=float(p["max_position_pct"]),
            stop_loss_pct=float(p["stop_loss_pct"]),
            take_profit_pct=float(p["take_profit_pct"]),
        )
        return costs, risk, bars_per_year, int(p["max_holding_bars"])

    def _load_history(self, sleeve: dict, end: pd.Timestamp | None = None) -> pd.DataFrame:
        end = end or pd.Timestamp.now(tz="UTC")
        start = end - pd.Timedelta(days=self.config.history_lookback_days)
        if sleeve["market"] == "stock":
            return AlpacaData().bars(
                sleeve["symbol"], start.isoformat(), end.isoformat(),
                timeframe="1Day", adjustment="all", feed="iex",
            )
        return BinanceData().bars(sleeve["symbol"], start.isoformat(), end.isoformat(), interval="1d")

    def process_sleeve(self, sleeve: dict, df: pd.DataFrame | None = None, now_iso: str | None = None) -> dict:
        now_iso = now_iso or utc_now_iso()
        now_ts = pd.Timestamp(now_iso)
        now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")
        df = self._load_history(sleeve, now_ts) if df is None else df.copy()
        if df is None or len(df) < self.config.minimum_warmup_bars:
            return {"sleeve_id":sleeve["sleeve_id"],"bars_processed":0,"reason":"insufficient_history"}
        v = validate_ohlcv(df)
        critical = sum(v[k] for k in [
            "duplicates","missing","bad_high","bad_low","nonpositive_price","negative_volume","non_monotonic_time"
        ])
        if critical:
            return {"sleeve_id":sleeve["sleeve_id"],"bars_processed":0,"reason":"invalid_ohlcv"}

        params = _parse_params(sleeve["params_json"])
        signal = signal_for(sleeve["strategy"], df, params).reindex(df.index).fillna(0.0)
        state = self.db.state(sleeve["sleeve_id"])
        registered = pd.Timestamp(sleeve["registered_at"])
        registered = registered.tz_localize("UTC") if registered.tzinfo is None else registered.tz_convert("UTC")
        last_processed = pd.Timestamp(state["last_processed_bar"]) if state.get("last_processed_bar") else None
        if last_processed is not None:
            last_processed = last_processed.tz_localize("UTC") if last_processed.tzinfo is None else last_processed.tz_convert("UTC")

        idx_utc = pd.DatetimeIndex(df.index)
        idx_utc = idx_utc.tz_localize("UTC") if idx_utc.tz is None else idx_utc.tz_convert("UTC")
        if sleeve["market"] == "crypto":
            current_date = now_ts.floor("D").date()
            closed_mask = np.array([ts.date() < current_date for ts in idx_utc])
        else:
            ny_today = now_ts.tz_convert("America/New_York").date()
            closed_mask = np.array([ts.tz_convert("America/New_York").date() < ny_today for ts in idx_utc])
        closed_index = df.index[closed_mask]
        eligible = closed_index[closed_index > registered]
        if last_processed is not None:
            eligible = eligible[eligible > last_processed]
        if len(eligible) == 0:
            return {"sleeve_id":sleeve["sleeve_id"],"bars_processed":0,"reason":"no_new_closed_bar"}

        costs, risk, _, max_holding = self._settings(sleeve["market"], sleeve["horizon"])
        cost_rate = costs.one_way_rate
        bars_processed = 0

        for ts in eligible:
            i = df.index.get_loc(ts)
            if not isinstance(i,(int,np.integer)) or i <= 0:
                continue
            o,h,l,c = map(float,[df.loc[ts,"open"],df.loc[ts,"high"],df.loc[ts,"low"],df.loc[ts,"close"]])
            cash = float(state["cash"]); qty = float(state["qty"])
            entry_fill = state.get("entry_fill")
            entry_cost_basis = float(state.get("entry_cost_basis") or 0.0)
            pending = int(state.get("pending_target") or 0)
            bars_in_position = int(state.get("bars_in_position") or 0)
            prior_signal_bar = state.get("last_signal_bar")
            forced_exit = False

            # Time-based exit is known before the current bar opens.
            if qty > 0 and bars_in_position >= max_holding:
                fill = o * (1-cost_rate)
                proceeds = qty*fill
                pnl = proceeds-entry_cost_basis
                cash += proceeds
                self.db.insert_trade({
                    "sleeve_id":sleeve["sleeve_id"],"bar_time":ts.isoformat(),"action":"SELL",
                    "fill_price":fill,"qty":qty,"realized_pnl":pnl,"reason":"TIME_EXIT",
                    "signal_bar":prior_signal_bar,"created_at":now_iso,
                })
                qty,entry_fill,entry_cost_basis,bars_in_position = 0.0,None,0.0,0
                forced_exit = True

            if not forced_exit:
                if pending == 1 and qty == 0:
                    fill = o*(1+cost_rate)
                    allocation = min(cash,float(sleeve["initial_capital"])*risk.max_position_pct)
                    new_qty = allocation/fill if fill > 0 else 0.0
                    if new_qty > 0:
                        spent = new_qty*fill
                        cash -= spent; qty = new_qty; entry_fill = fill; entry_cost_basis = spent; bars_in_position = 0
                        self.db.insert_trade({
                            "sleeve_id":sleeve["sleeve_id"],"bar_time":ts.isoformat(),"action":"BUY",
                            "fill_price":fill,"qty":qty,"realized_pnl":0.0,"reason":"SIGNAL",
                            "signal_bar":prior_signal_bar,"created_at":now_iso,
                        })
                elif pending == 0 and qty > 0:
                    fill = o*(1-cost_rate)
                    proceeds = qty*fill; pnl = proceeds-entry_cost_basis; cash += proceeds
                    self.db.insert_trade({
                        "sleeve_id":sleeve["sleeve_id"],"bar_time":ts.isoformat(),"action":"SELL",
                        "fill_price":fill,"qty":qty,"realized_pnl":pnl,"reason":"SIGNAL",
                        "signal_bar":prior_signal_bar,"created_at":now_iso,
                    })
                    qty,entry_fill,entry_cost_basis,bars_in_position = 0.0,None,0.0,0

            if qty > 0 and entry_fill is not None:
                stop = float(entry_fill)*(1-risk.stop_loss_pct)
                target = float(entry_fill)*(1+risk.take_profit_pct)
                exit_px = None; reason = None
                if l <= stop:
                    exit_px,reason = stop*(1-cost_rate),"STOP"
                elif h >= target:
                    exit_px,reason = target*(1-cost_rate),"TAKE_PROFIT"
                if exit_px is not None:
                    proceeds = qty*exit_px; pnl = proceeds-entry_cost_basis; cash += proceeds
                    self.db.insert_trade({
                        "sleeve_id":sleeve["sleeve_id"],"bar_time":ts.isoformat(),"action":"SELL",
                        "fill_price":exit_px,"qty":qty,"realized_pnl":pnl,"reason":reason,
                        "signal_bar":prior_signal_bar,"created_at":now_iso,
                    })
                    qty,entry_fill,entry_cost_basis,bars_in_position = 0.0,None,0.0,0

            equity = cash + qty*c
            self.db.upsert_equity({
                "sleeve_id":sleeve["sleeve_id"],"bar_time":ts.isoformat(),"cash":cash,
                "qty":qty,"close_price":c,"equity":equity,
            })
            current_target = int(float(signal.loc[ts]) > 0)
            if qty > 0:
                bars_in_position += 1
            state = {
                "cash":cash,"qty":qty,"entry_fill":entry_fill,"entry_cost_basis":entry_cost_basis,
                "pending_target":current_target,"bars_in_position":bars_in_position,
                "last_signal_bar":ts.isoformat(),"last_processed_bar":ts.isoformat(),"updated_at":now_iso,
            }
            self.db.update_state(sleeve["sleeve_id"],state)
            bars_processed += 1

        return {"sleeve_id":sleeve["sleeve_id"],"bars_processed":bars_processed,"reason":"ok"}

    def run_once(self, data_override: dict[str,pd.DataFrame] | None = None, now_iso: str | None = None) -> dict:
        now_iso = now_iso or utc_now_iso()
        run_id = self.db.start_run(now_iso)
        checked = processed = 0; errors = []
        for sleeve in self.db.sleeves("ACTIVE"):
            checked += 1
            try:
                df = None if data_override is None else data_override.get(sleeve["sleeve_id"])
                r = self.process_sleeve(sleeve,df=df,now_iso=now_iso)
                processed += int(r.get("bars_processed",0))
            except Exception as e:
                errors.append(f"{sleeve['sleeve_id']}: {type(e).__name__}: {e}")
        status = "OK" if not errors else "PARTIAL"
        self.db.finish_run(run_id,utc_now_iso(),status,checked,processed,"\n".join(errors) or None)
        return {"run_id":run_id,"status":status,"sleeves_checked":checked,"bars_processed":processed,"errors":errors}

def register_three_horizons(forward_db: ForwardDB, horizon_db: HorizonDB,
                            initial_capital_per_sleeve: float = 100000,
                            registered_at: str | None = None) -> pd.DataFrame:
    registered_at = registered_at or utc_now_iso()
    rows = []
    for base in forward_db.candidates("ACTIVE"):
        base_params = _parse_params(base["params_json"])
        for horizon in HORIZON_ORDER:
            params = params_for_horizon(base["strategy"],horizon,base_params)
            sid = sleeve_id(base["candidate_id"],horizon,params)
            row = {
                "sleeve_id":sid,"base_candidate_id":base["candidate_id"],"market":base["market"],
                "symbol":base["symbol"],"strategy":base["strategy"],"horizon":horizon,
                "params":params,"registered_at":registered_at,"initial_capital":float(initial_capital_per_sleeve),
                "research_grade":base.get("research_grade"),"status":"ACTIVE",
                "notes":"Independent daily-bar horizon sleeve. Short=days, medium=weeks, long=months.",
            }
            horizon_db.register_sleeve(row); rows.append(row)
    return pd.DataFrame(rows)

def horizon_scorecard(db: HorizonDB, sleeve: dict, bars_per_year: int) -> dict:
    eq_rows = db.equity(sleeve["sleeve_id"]); tr_rows = db.trades(sleeve["sleeve_id"])
    profile = HORIZON_PROFILES[sleeve["horizon"]]
    if not eq_rows:
        return {
            "sleeve_id":sleeve["sleeve_id"],"market":sleeve["market"],"symbol":sleeve["symbol"],
            "strategy":sleeve["strategy"],"horizon":sleeve["horizon"],"horizon_label":HORIZON_LABELS[sleeve["horizon"]],
            "forward_bars":0,"forward_days":0,"closed_trades":0,"total_return":0.0,"sharpe":np.nan,
            "max_drawdown":0.0,"win_rate":np.nan,"profit_factor":np.nan,"evidence":0.0,"horizon_score":0.0,
            "research_grade":sleeve.get("research_grade"),"status":sleeve["status"],
        }
    eqdf = pd.DataFrame(eq_rows); eqdf["bar_time"] = pd.to_datetime(eqdf["bar_time"],utc=True)
    equity = pd.Series(eqdf["equity"].to_numpy(float),index=eqdf["bar_time"])
    tdf = pd.DataFrame(tr_rows)
    metrics_trades = tdf if len(tdf) else pd.DataFrame()
    m = performance_metrics(equity,metrics_trades,bars_per_year,0.0)
    days = max((eqdf["bar_time"].max()-eqdf["bar_time"].min()).days+1,1)
    closed = int(m.get("closed_trades",0))
    sh = float(np.nan_to_num(m.get("sharpe",np.nan),nan=-2.0,posinf=3.0,neginf=-3.0))
    ret = float(np.nan_to_num(m.get("total_return",np.nan),nan=-1.0))
    mdd = abs(float(np.nan_to_num(m.get("max_drawdown",np.nan),nan=1.0)))
    time_ev = min(days/max(float(profile["evidence_days"]),1),1.0)
    trade_ev = min(closed/max(float(profile["evidence_trades"]),1),1.0)
    evidence = 0.6*time_ev+0.4*trade_ev
    quality = 45*np.tanh(sh/2)+25*np.tanh(ret*3)+20*(1-min(mdd,1))+10
    quality = float(np.clip(quality,0,100)); score = quality*(0.35+0.65*evidence)
    return {
        "sleeve_id":sleeve["sleeve_id"],"market":sleeve["market"],"symbol":sleeve["symbol"],
        "strategy":sleeve["strategy"],"horizon":sleeve["horizon"],"horizon_label":HORIZON_LABELS[sleeve["horizon"]],
        "forward_bars":len(eqdf),"forward_days":days,"closed_trades":closed,"total_return":m.get("total_return"),
        "sharpe":m.get("sharpe"),"max_drawdown":m.get("max_drawdown"),"win_rate":m.get("win_rate"),
        "profit_factor":m.get("profit_factor"),"evidence":float(evidence),"horizon_score":float(np.clip(score,0,100)),
        "research_grade":sleeve.get("research_grade"),"status":sleeve["status"],
    }

def rank_horizons(db: HorizonDB, stock_bars_per_year: int = 252, crypto_bars_per_year: int = 365) -> pd.DataFrame:
    rows = []
    for sleeve in db.sleeves():
        bpy = stock_bars_per_year if sleeve["market"] == "stock" else crypto_bars_per_year
        rows.append(horizon_scorecard(db,sleeve,bpy))
    if not rows: return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["horizon_order"] = out["horizon"].map({"short":0,"medium":1,"long":2})
    return out.sort_values(["horizon_order","horizon_score","evidence"],ascending=[True,False,False]).drop(columns="horizon_order").reset_index(drop=True)

def best_horizon_by_symbol(ranking: pd.DataFrame) -> pd.DataFrame:
    if ranking is None or ranking.empty: return pd.DataFrame()
    return (ranking.sort_values(["symbol","horizon_score","evidence"],ascending=[True,False,False])
            .drop_duplicates(["market","symbol"]).reset_index(drop=True))
