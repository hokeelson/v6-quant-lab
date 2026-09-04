from __future__ import annotations

import math
import os
import time
from datetime import time as dt_time

import numpy as np
import pandas as pd
import requests

from .backtest import ExecutionCosts, RiskRules, run_backtest
from .decision_engine import HORIZON_SPECS, PARAM_GRIDS, _grid, decision_for, market_regime, regime_fit
from .market_cache import HISTORY_DAYS, TIMEFRAME_MAP, MarketCache, _utc
from .research import robustness_score, strategy_signal
from .risk_sizing import active_entry_sizing
from .entry_gate import safe_entry_sizing
from .simulation_db import SimulationDB, now_iso
from .simulation_engine import SimulationLab

TW_MARKET = "twstock"
TW_BARS_PER_YEAR = {"short": 1134, "medium": 252, "long": 52}
TW_MAX_HOLDING = {"short": 36, "medium": 50, "long": 78}
TW_CACHE_TIMEFRAME = {"short": "1h", "medium": "1d", "long": "1wk"}
TW_YAHOO_INTERVAL = {"short": "1h", "medium": "1d", "long": "1wk"}
TW_REFRESH_SECONDS = {"short": 300, "medium": 1800, "long": 21600}

# Keep compatibility with analytics code that imports TIMEFRAME_MAP from market_cache.
TIMEFRAME_MAP.update({
    (TW_MARKET, "short"): ("1h", "1h"),
    (TW_MARKET, "medium"): ("1d", "1d"),
    (TW_MARKET, "long"): ("1wk", "1wk"),
})


def normalize_tw_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return s
    if s.endswith(".TW") or s.endswith(".TWO"):
        return s
    if s.isdigit():
        return f"{s}.TW"
    return s


class TaiwanData:
    """Taiwan OHLCV via Yahoo Finance chart endpoint. No API key is required."""

    base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

    def bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        symbol = normalize_tw_symbol(symbol)
        s = _utc(start)
        e = _utc(end)
        params = {
            "period1": int(s.timestamp()),
            "period2": int(e.timestamp()),
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
        headers = {"User-Agent": "Mozilla/5.0 V6-Quant-Lab/1.0"}
        last_error = None
        for attempt in range(3):
            try:
                r = requests.get(f"{self.base_url}/{symbol}", params=params, headers=headers, timeout=30)
                r.raise_for_status()
                chart = (r.json().get("chart") or {})
                if chart.get("error"):
                    raise RuntimeError(str(chart["error"]))
                results = chart.get("result") or []
                if not results:
                    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                result = results[0]
                stamps = result.get("timestamp") or []
                quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
                if not stamps:
                    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
                df = pd.DataFrame({
                    "timestamp": pd.to_datetime(stamps, unit="s", utc=True),
                    "open": quote.get("open", []),
                    "high": quote.get("high", []),
                    "low": quote.get("low", []),
                    "close": quote.get("close", []),
                    "volume": quote.get("volume", []),
                }).set_index("timestamp")
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                return df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Taiwan market data fetch failed for {symbol}: {last_error}")


class TaiwanMarketCache(MarketCache):
    def ensure(self, market: str, symbol: str, horizon: str, now=None,
               force_history: bool = False, min_refresh_seconds: int | None = None) -> dict:
        if market != TW_MARKET:
            return super().ensure(market, symbol, horizon, now, force_history, min_refresh_seconds)

        now = _utc(now or pd.Timestamp.now(tz="UTC"))
        symbol = normalize_tw_symbol(symbol)
        tf_key = TW_CACHE_TIMEFRAME[horizon]
        interval = TW_YAHOO_INTERVAL[horizon]
        last = self.last_timestamp(market, symbol, tf_key)
        history_start = now - pd.Timedelta(days=HISTORY_DAYS[horizon])
        if min_refresh_seconds is None:
            min_refresh_seconds = TW_REFRESH_SECONDS[horizon]
        last_attempt = self._last_attempt(market, symbol, horizon)
        if (not force_history) and last_attempt is not None and (now - last_attempt).total_seconds() < min_refresh_seconds:
            return {"fetched": 0, "api_called": False, "timeframe": tf_key,
                    "data": self.get(market, symbol, tf_key, history_start, now)}

        if force_history or last is None:
            start = history_start
        else:
            overlap = {
                "short": pd.Timedelta(hours=3),
                "medium": pd.Timedelta(days=3),
                "long": pd.Timedelta(days=14),
            }[horizon]
            start = max(history_start, _utc(last) - overlap)
        if start >= now:
            return {"fetched": 0, "api_called": False, "timeframe": tf_key,
                    "data": self.get(market, symbol, tf_key, history_start, now)}

        self._set_attempt(market, symbol, horizon, now)
        new = TaiwanData().bars(symbol, start.isoformat(), now.isoformat(), interval=interval)
        fetched = self.upsert(market, symbol, tf_key, new)
        return {"fetched": fetched, "api_called": True, "timeframe": tf_key,
                "data": self.get(market, symbol, tf_key, history_start, now)}

    @staticmethod
    def closed_only(df: pd.DataFrame, market: str, horizon: str, now=None) -> pd.DataFrame:
        if market != TW_MARKET:
            return MarketCache.closed_only(df, market, horizon, now)
        if df is None or df.empty:
            return df

        now_utc = _utc(now or pd.Timestamp.now(tz="UTC"))
        now_tw = now_utc.tz_convert("Asia/Taipei")
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")

        if horizon == "short":
            mask = idx + pd.Timedelta(hours=1) <= now_utc
        elif horizon == "medium":
            local = idx.tz_convert("Asia/Taipei")
            mask = []
            for x in local:
                if x.date() < now_tw.date():
                    mask.append(True)
                elif x.date() == now_tw.date() and now_tw.time() >= dt_time(13, 40):
                    mask.append(True)
                else:
                    mask.append(False)
        else:
            # Yahoo weekly bars are anchored at the beginning of the week.
            local = idx.tz_convert("Asia/Taipei")
            mask = []
            for x in local:
                d = x.date()
                days_to_friday = (4 - d.weekday()) % 7
                friday = pd.Timestamp(d + pd.Timedelta(days=days_to_friday), tz="Asia/Taipei")
                weekly_close = friday + pd.Timedelta(hours=13, minutes=40)
                mask.append(now_tw >= weekly_close)
        return df.loc[mask].copy()


class TaiwanSimulationDB(SimulationDB):
    def ensure_accounts(self, initial_equity: float = 100000.0):
        rows = []
        with self._c() as c:
            for market in ("stock", "crypto", TW_MARKET):
                for horizon in ("short", "medium", "long"):
                    aid = f"{market}_{horizon}"
                    c.execute(
                        "INSERT OR IGNORE INTO accounts(account_id,market,horizon,initial_equity,cash,created_at) VALUES(?,?,?,?,?,?)",
                        (aid, market, horizon, float(initial_equity), float(initial_equity), now_iso()),
                    )
                    rows.append(dict(c.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone()))
        return rows


def _tw_costs() -> ExecutionCosts:
    # Taiwan cash-stock model: broker commission on both sides and the regular
    # 0.3% securities transaction tax only on the sell side. Day-trade tax relief
    # is intentionally not assumed because this simulator is not day-trade-only.
    return ExecutionCosts(commission_bps=14.25, slippage_bps=5.0, spread_bps=4.0, sell_tax_bps=30.0)


def calibrate_twstock(df: pd.DataFrame, horizon: str, initial_capital: float = 100000.0):
    spec = HORIZON_SPECS[horizon]
    if len(df) < spec["warmup"]:
        raise ValueError(f"Need at least {spec['warmup']} closed bars")
    data = df.tail(spec["calibration"]).copy()
    split = max(spec["warmup"] // 2, int(len(data) * (1 - spec["oos_frac"])))
    if split >= len(data) - 30:
        split = max(60, len(data) - max(30, int(len(data) * spec["oos_frac"])))
    train = data.iloc[:split]
    test = data.iloc[split:]
    bpy = TW_BARS_PER_YEAR[horizon]
    costs = _tw_costs()
    risk = RiskRules(max_position_pct=0.25, stop_loss_pct=0.12, take_profit_pct=0.30)
    regime = market_regime(data)
    best = None
    rows = []
    for strat in PARAM_GRIDS[horizon]:
        for p in _grid(strat, horizon):
            try:
                tr = run_backtest(train, strategy_signal(strat, train, p), initial_capital, costs, risk, bpy)
                te = run_backtest(test, strategy_signal(strat, test, p), initial_capital, costs, risk, bpy)
            except Exception:
                continue
            ts = robustness_score(tr["metrics"], spec["min_trades"])
            oscore = robustness_score(te["metrics"], spec["min_trades"])
            rf = regime_fit(strat, regime)
            gap = abs(ts - oscore)
            stability = max(0.0, 100 - gap)
            sample = min(1.0, float(te["metrics"].get("closed_trades", 0)) / max(1, spec["min_trades"]))
            score = 0.55 * oscore + 0.20 * ts + 0.10 * stability + 0.10 * (rf * 100) + 0.05 * (sample * 100)
            row = {"strategy": strat, "params": p, "train_score": ts, "oos_score": oscore,
                   "regime_fit": rf, "stability": stability, "sample": sample,
                   "score": score, "oos_metrics": te["metrics"]}
            rows.append(row)
            if best is None or score > best["score"]:
                best = row
    if best is None:
        raise RuntimeError("No strategy could be calibrated")
    ranked = sorted(rows, key=lambda x: x["score"], reverse=True)[:10]
    return {
        "strategy": best["strategy"], "params": best["params"],
        "calibration_score": float(best["score"]), "oos_score": float(best["oos_score"]),
        "train_score": float(best["train_score"]), "regime_fit": float(best["regime_fit"]),
        "calibrated_through": data.index[-1].isoformat(),
        "diagnostics": {
            "regime": regime, "stability": best["stability"], "sample": best["sample"],
            "oos_metrics": best["oos_metrics"],
            "top10": [{"strategy": r["strategy"], "params": r["params"],
                       "score": r["score"], "oos_score": r["oos_score"]} for r in ranked],
            "market": TW_MARKET, "bars_per_year": bpy,
        },
    }


def decision_twstock(df: pd.DataFrame, horizon: str, model: dict, equity: float):
    # Reuse the proven signal/confidence logic, but Taiwan cash equities run at 1x.
    d = decision_for(df, "stock", horizon, model, equity)
    raw = float((d.get("diagnostics") or {}).get("raw_notional", d.get("requested_notional", 0)) or 0)
    base = float((d.get("diagnostics") or {}).get("base_cap", d.get("requested_notional", 0)) or 0)
    d["requested_notional"] = float(min(raw, base))
    d["leverage"] = 1.0
    d["max_holding_bars"] = TW_MAX_HOLDING[horizon]
    d.setdefault("diagnostics", {})["taiwan_cash_only"] = True
    return d


class TaiwanSimulationLab(SimulationLab):
    def import_assets(self, rows):
        n = 0
        for r in rows:
            market = str(r.get("market", ""))
            symbol = str(r.get("symbol", "")).upper()
            if self.single_crypto_account and market != "crypto":
                continue
            if market in ("stock", "crypto", TW_MARKET) and symbol:
                if market == TW_MARKET:
                    symbol = normalize_tw_symbol(symbol)
                self.db.add_asset(market, symbol)
                n += 1
        return n

    def calibrate(self, market, symbol, horizon, now=None, force_history=False):
        if market != TW_MARKET:
            return super().calibrate(market, symbol, horizon, now, force_history)
        symbol = normalize_tw_symbol(symbol)
        pack = self.cache.ensure(market, symbol, horizon, now, force_history)
        df = self.cache.closed_only(pack["data"], market, horizon, now)
        model = calibrate_twstock(df, horizon, self.initial_equity)
        model.update({"market": market, "symbol": symbol, "horizon": horizon, "updated_at": now_iso()})
        self.db.save_model(model)
        return {"market": market, "symbol": symbol, "horizon": horizon, "fetched": pack["fetched"], **model}

    def _cost_rate(self, market):
        if market == TW_MARKET:
            return _tw_costs().one_way_rate
        return super()._cost_rate(market)

    def _buy_cost_rate(self, market):
        if market == TW_MARKET:
            return _tw_costs().buy_rate
        return super()._buy_cost_rate(market)

    def _sell_cost_rate(self, market):
        if market == TW_MARKET:
            return _tw_costs().sell_rate
        return super()._sell_cost_rate(market)

    def _accrue_financing(self, aid, market, horizon):
        if market == TW_MARKET:
            return 0.0
        return super()._accrue_financing(aid, market, horizon)

    def _margin_check(self, aid, market, horizon, ts):
        if market == TW_MARKET:
            return False
        return super()._margin_check(aid, market, horizon, ts)

    def _execute_pending(self, aid, market, symbol, ts, row):
        if market != TW_MARKET:
            return super()._execute_pending(aid, market, symbol, ts, row)
        o = self.db.pending_order(aid, symbol)
        if not o:
            return None
        decision_context = self.db.decision(o.get("decision_id")) or {}
        acct = self.db.account(aid)
        pos = self.db.position(aid, symbol)
        open_px = float(row.open)
        hz = decision_context.get("horizon", aid.rsplit("_", 1)[-1])
        if o["side"] == "BUY" and pos is not None:
            self.db.cancel_order(o["order_id"], "STALE_BUY_POSITION_EXISTS")
            self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "ORDER_CANCELLED", "Stale BUY cancelled because position already exists",
                                   {"cancel_reason": "STALE_BUY_POSITION_EXISTS", "broker_order_api_calls": 0})
            return "CANCELLED"
        if o["side"] == "SELL" and pos is None:
            self.db.cancel_order(o["order_id"], "STALE_SELL_NO_POSITION")
            self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "ORDER_CANCELLED", "Stale SELL cancelled because position no longer exists",
                                   {"cancel_reason": "STALE_SELL_NO_POSITION", "broker_order_api_calls": 0})
            return "CANCELLED"
        if o["side"] == "BUY" and pos is None:
            cash, gross, equity = self._account_marks(aid, {symbol: open_px})
            original_notional = float(o["requested_notional"] or 0)
            sizing = safe_entry_sizing(active_entry_sizing,self.db, self.cache, market, symbol, hz, decision_context, original_notional)
            risk_adjusted = float(sizing.get("adjusted_notional", original_notional) or 0)
            notional = min(risk_adjusted, max(0.0, cash))
            rate = self._buy_cost_rate(market)
            fill = open_px * (1 + rate)
            qty = math.floor(notional / fill) if fill > 0 else 0
            if qty <= 0:
                cancel_reason = "ENTRY_GATE_BLOCKED" if not sizing.get("entry_allowed") else "INSUFFICIENT_CASH_FOR_BOARD_LOT"
                self.db.cancel_order(o["order_id"], cancel_reason)
                self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "ORDER_CANCELLED", "Pending Taiwan BUY cancelled before fill",
                                       {**sizing, "cash_room": max(0.0, cash), "risk_adjusted_notional": risk_adjusted,
                                        "requested_notional": original_notional, "cancel_reason": cancel_reason, "broker_order_api_calls": 0,
                                        "order_id": o["order_id"], "decision_id": o.get("decision_id")})
                return "CANCELLED"
            spent = qty * fill
            fees = qty * open_px * rate
            position={
                "account_id": aid, "symbol": symbol, "qty": qty, "avg_entry": fill,
                "entry_bar": ts.isoformat(), "strategy": decision_context.get("strategy"),
                "horizon": hz, "regime_entry": decision_context.get("regime"),
                "stop_price": fill * (1 - float(decision_context.get("stop_distance", 0.08))),
                "target_price": fill * (1 + float(decision_context.get("target_distance", 0.20))),
                "max_holding_bars": int((decision_context.get("diagnostics") or {}).get("max_holding_bars", TW_MAX_HOLDING[hz])),
                "bars_held": 0, "leverage_at_entry": 1.0,
            }
            if not self.db.fill_buy_atomic(aid, o["order_id"], ts.isoformat(), fill, fees, fill - open_px, cash - spent, position):
                return None
            self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "RISK_SIZING", "Active portfolio/strategy sizing applied", {
                **sizing, "cash_room": max(0.0, cash), "filled_notional": spent, "fill_price": fill,
                "order_id": o["order_id"], "decision_id": o.get("decision_id"),
                "broker_order_api_calls": 0,
            })
            return "BUY"
        if o["side"] == "SELL" and pos is not None:
            rate = self._sell_cost_rate(market)
            fill = open_px * (1 - rate)
            proceeds = float(pos["qty"]) * fill
            cash = float(acct["cash"]) + proceeds
            pnl = float(pos["qty"]) * (fill - float(pos["avg_entry"]))
            ret = fill / float(pos["avg_entry"]) - 1
            trade={
                "account_id": aid, "symbol": symbol, "entry_bar": pos["entry_bar"],
                "exit_bar": ts.isoformat(), "qty": pos["qty"], "entry_price": pos["avg_entry"],
                "exit_price": fill, "realized_pnl": pnl, "return_pct": ret,
                "strategy": pos["strategy"], "horizon": pos["horizon"],
                "regime_entry": pos.get("regime_entry"), "exit_reason": o["reason"] or "SIGNAL_EXIT",
                "leverage": 1.0,
            }
            if not self.db.fill_sell_atomic(aid, o["order_id"], ts.isoformat(), fill, proceeds * rate, open_px - fill, cash, trade, symbol):
                return None
            if pnl < 0:
                self.db.add_diagnostic(aid, symbol, pos["horizon"], ts.isoformat(), "LOSS", "Losing model exit",
                                       {"pnl": pnl, "return_pct": ret, "strategy": pos["strategy"],
                                        "regime_entry": pos.get("regime_entry"), "leverage": 1.0,
                                        "bars_held": pos["bars_held"], "exit_reason": o["reason"] or "SIGNAL_EXIT"})
            return "SELL"
        return None

    def process_asset_horizon(self, market, symbol, horizon, now=None):
        if market != TW_MARKET:
            return super().process_asset_horizon(market, symbol, horizon, now)
        aid = f"{market}_{horizon}"
        symbol = normalize_tw_symbol(symbol)
        pack = self.cache.ensure(market, symbol, horizon, now)
        df = self.cache.closed_only(pack["data"], market, horizon, now)
        spec = HORIZON_SPECS[horizon]
        if len(df) < spec["warmup"]:
            return {"processed": 0, "reason": "insufficient_history", "fetched": pack["fetched"],
                    "api_called": pack.get("api_called", False)}
        model = self.db.model(market, symbol, horizon)
        if model is None:
            self.calibrate(market, symbol, horizon, now)
            model = self.db.model(market, symbol, horizon)
        last = self.db.last_processed(aid, symbol)
        eligible = df if last is None else df[df.index > pd.Timestamp(last)]
        if last is None:
            self.db.set_last_processed(aid, symbol, df.index[-1].isoformat())
            return {"processed": 0, "reason": "forward_registered", "fetched": pack["fetched"],
                    "api_called": pack.get("api_called", False)}
        if eligible.empty:
            return {"processed": 0, "reason": "no_new_closed_bar", "fetched": pack["fetched"],
                    "api_called": pack.get("api_called", False)}

        processed = 0
        for ts, row in eligible.iterrows():
            self.db.set_mark(aid, symbol, ts.isoformat(), float(row.open))
            self._execute_pending(aid, market, symbol, ts, row)
            self._protect_position(aid, market, symbol, ts, row)
            self.db.set_mark(aid, symbol, ts.isoformat(), float(row.close))
            hist = df.loc[:ts]
            cash, gross, equity = self._account_marks(aid)
            dec = decision_twstock(hist, horizon, model, max(equity, 1.0))
            dec["horizon"] = horizon
            dec.setdefault("diagnostics", {})["max_holding_bars"] = dec.pop("max_holding_bars")
            pos = self.db.position(aid, symbol)
            if pos is not None and dec["action"] == "EXIT" and not self.db.pending_order(aid, symbol):
                did = self.db.add_decision({"account_id": aid, "market": market, "symbol": symbol,
                                            "horizon": horizon, "bar_time": ts.isoformat(), **dec})
                self.db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL",
                                   "created_bar": ts.isoformat(), "requested_notional": 0.0,
                                   "qty": pos["qty"], "reason": "MODEL_EXIT", "decision_id": did})
            elif pos is None and dec["action"] == "ENTER" and not self.db.pending_order(aid, symbol):
                did = self.db.add_decision({"account_id": aid, "market": market, "symbol": symbol,
                                            "horizon": horizon, "bar_time": ts.isoformat(), **dec})
                self.db.add_order({"account_id": aid, "symbol": symbol, "side": "BUY",
                                   "created_bar": ts.isoformat(), "requested_notional": dec["requested_notional"],
                                   "qty": None, "reason": "MODEL_ENTER", "decision_id": did})
            else:
                self.db.add_decision({"account_id": aid, "market": market, "symbol": symbol,
                                      "horizon": horizon, "bar_time": ts.isoformat(), **dec})
            cash, gross, equity = self._account_marks(aid)
            peak = self.db.peak_equity(aid) or float(self.db.account(aid)["initial_equity"])
            peak = max(peak, equity)
            dd = equity / peak - 1 if peak > 0 else 0
            lev = gross / equity if equity > 0 else float("inf")
            self.db.save_equity(aid, ts.isoformat(), equity, cash, gross, lev, dd)
            self.db.set_last_processed(aid, symbol, ts.isoformat())
            processed += 1
        return {"processed": processed, "fetched": pack["fetched"], "api_called": pack.get("api_called", False)}
