from __future__ import annotations
import json, math
import numpy as np
import pandas as pd

from .backtest import ExecutionCosts
from .decision_engine import HORIZON_SPECS, calibrate_asset, decision_for
from .market_cache import MarketCache
from .risk_sizing import active_entry_sizing
from .simulation_db import SimulationDB, now_iso


class SimulationLab:
    """Local six-account forward simulator. No broker order API is used."""
    def __init__(self, db=None, cache=None, initial_equity=100000.0):
        self.db=db or SimulationDB("simulation_lab.sqlite3")
        self.cache=cache or MarketCache("market_cache.sqlite3")
        self.initial_equity=float(initial_equity)
        self.db.ensure_accounts(self.initial_equity)

    def import_assets(self, rows):
        n=0
        for r in rows:
            market=str(r.get("market","")); symbol=str(r.get("symbol","")).upper()
            if market in ("stock","crypto") and symbol:
                self.db.add_asset(market,symbol); n+=1
        return n

    def calibrate(self, market, symbol, horizon, now=None, force_history=False):
        pack=self.cache.ensure(market,symbol,horizon,now,force_history)
        df=self.cache.closed_only(pack["data"],market,horizon,now)
        model=calibrate_asset(df,market,horizon,self.initial_equity)
        model.update({"market":market,"symbol":symbol.upper(),"horizon":horizon,"updated_at":now_iso()})
        self.db.save_model(model)
        return {"market":market,"symbol":symbol.upper(),"horizon":horizon,"fetched":pack["fetched"],**model}

    def calibrate_all(self, now=None):
        out=[]; errors=[]
        for a in self.db.assets():
            for hz in ("short","medium","long"):
                try: out.append(self.calibrate(a["market"],a["symbol"],hz,now))
                except Exception as e: errors.append({"market":a["market"],"symbol":a["symbol"],"horizon":hz,"error":f"{type(e).__name__}: {e}"})
        return {"calibrated":len(out),"errors":errors,"results":out}

    def _cost_rate(self,market):
        c=ExecutionCosts(0,3,2) if market=="stock" else ExecutionCosts(10,5,4)
        return c.one_way_rate

    def _account_marks(self, aid, prices=None):
        acct=self.db.account(aid); cash=float(acct["cash"]); gross=0.0
        marks=self.db.marks(aid); marks.update(prices or {})
        for p in self.db.positions(aid):
            px=float(marks.get(p["symbol"],p["avg_entry"])); gross += float(p["qty"])*px
        equity=cash+gross
        return cash,gross,equity

    def _execute_pending(self, aid, market, symbol, ts, row):
        o=self.db.pending_order(aid,symbol)
        if not o:return None
        decision_context=self.db.decision(o.get("decision_id")) or {}
        acct=self.db.account(aid); pos=self.db.position(aid,symbol); rate=self._cost_rate(market)
        open_px=float(row.open)
        if o["side"]=="BUY" and pos is None:
            # Enforce gross leverage cap at fill using current account marks.
            prices={symbol:open_px}; cash,gross,equity=self._account_marks(aid,prices)
            hz=decision_context.get("horizon",aid.split("_",1)[1])
            maxlev=HORIZON_SPECS[hz]["max_leverage"]
            room=max(0.0,equity*maxlev-gross)
            original_notional=float(o["requested_notional"] or 0)
            sizing=active_entry_sizing(self.db,self.cache,market,symbol,hz,decision_context,original_notional)
            risk_adjusted=float(sizing.get("adjusted_notional",original_notional) or 0)
            notional=min(risk_adjusted,room)
            if notional<=0:
                cancel_reason = "RISK_SIZING_ZERO_NOTIONAL" if risk_adjusted <= 0 else "NO_LEVERAGE_ROOM"
                self.db.cancel_order(o["order_id"], cancel_reason)
                self.db.add_diagnostic(aid,symbol,hz,ts.isoformat(),"ORDER_CANCELLED","Pending BUY cancelled before fill",{
                    **sizing,"leverage_room":room,"risk_adjusted_notional":risk_adjusted,
                    "requested_notional":original_notional,"cancel_reason":cancel_reason,
                    "broker_order_api_calls":0,
                })
                return "CANCELLED"
            fill=open_px*(1+rate); qty=notional/fill; fees=notional*rate
            self.db.set_cash(aid,cash-notional)
            self.db.upsert_position({"account_id":aid,"symbol":symbol,"qty":qty,"avg_entry":fill,"entry_bar":ts.isoformat(),
                "strategy":decision_context.get("strategy"),"horizon":hz,"regime_entry":decision_context.get("regime"),
                "stop_price":fill*(1-float(decision_context.get("stop_distance",0.08))),"target_price":fill*(1+float(decision_context.get("target_distance",0.20))),
                "max_holding_bars":int(decision_context.get("diagnostics",{}).get("max_holding_bars",HORIZON_SPECS[hz]["max_holding_stock"] if market=="stock" else HORIZON_SPECS[hz]["max_holding_crypto"])),"bars_held":0,"leverage_at_entry":float(decision_context.get("leverage",1.0))})
            self.db.fill_order(o["order_id"],ts.isoformat(),fill,fees,fill-open_px)
            self.db.add_diagnostic(aid,symbol,hz,ts.isoformat(),"RISK_SIZING","Active portfolio/strategy sizing applied",{
                **sizing,"leverage_room":room,"filled_notional":notional,"fill_price":fill,
                "broker_order_api_calls":0,
            })
            return "BUY"
        if o["side"]=="SELL" and pos is not None:
            fill=open_px*(1-rate); proceeds=float(pos["qty"])*fill; cash=float(acct["cash"])+proceeds
            pnl=float(pos["qty"])*(fill-float(pos["avg_entry"])); ret=fill/float(pos["avg_entry"])-1
            self.db.set_cash(aid,cash); self.db.fill_order(o["order_id"],ts.isoformat(),fill,proceeds*rate,open_px-fill)
            self.db.add_trade({"account_id":aid,"symbol":symbol,"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,
                "strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":o["reason"] or "SIGNAL_EXIT","leverage":pos["leverage_at_entry"]})
            if pnl < 0:
                self.db.add_diagnostic(aid,symbol,pos["horizon"],ts.isoformat(),"LOSS","Losing model exit",{"pnl":pnl,"return_pct":ret,"strategy":pos["strategy"],"regime_entry":pos.get("regime_entry"),"leverage":pos["leverage_at_entry"],"bars_held":pos["bars_held"],"exit_reason":o["reason"] or "SIGNAL_EXIT"})
            self.db.delete_position(aid,symbol); return "SELL"
        return None

    def _protect_position(self, aid, market, symbol, ts, row):
        pos=self.db.position(aid,symbol)
        if not pos:return None
        pos["bars_held"]=int(pos["bars_held"])+1
        exit_px=None; reason=None
        # conservative same-bar tie-break: stop before target
        if float(row.low)<=float(pos["stop_price"]): exit_px=float(pos["stop_price"]); reason="ATR_STOP"
        elif float(row.high)>=float(pos["target_price"]): exit_px=float(pos["target_price"]); reason="ATR_TARGET"
        elif pos["bars_held"]>=int(pos["max_holding_bars"]): exit_px=float(row.close); reason="TIME_EXIT"
        if exit_px is None:
            self.db.upsert_position(pos); return None
        rate=self._cost_rate(market); fill=exit_px*(1-rate); acct=self.db.account(aid)
        proceeds=float(pos["qty"])*fill; pnl=float(pos["qty"])*(fill-float(pos["avg_entry"])); ret=fill/float(pos["avg_entry"])-1
        self.db.set_cash(aid,float(acct["cash"])+proceeds)
        self.db.add_trade({"account_id":aid,"symbol":symbol,"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,
            "strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":reason,"leverage":pos["leverage_at_entry"]})
        self.db.delete_position(aid,symbol)
        self.db.add_diagnostic(aid,symbol,pos["horizon"],ts.isoformat(),"EXIT",reason,{"pnl":pnl,"return_pct":ret})
        if pnl < 0:
            self.db.add_diagnostic(aid,symbol,pos["horizon"],ts.isoformat(),"LOSS","Losing protective/time exit",{"pnl":pnl,"return_pct":ret,"strategy":pos["strategy"],"regime_entry":pos.get("regime_entry"),"leverage":pos["leverage_at_entry"],"bars_held":pos["bars_held"],"exit_reason":reason})
        return reason

    def _accrue_financing(self, aid, market, horizon):
        acct=self.db.account(aid); cash=float(acct["cash"])
        if cash>=0:return 0.0
        bpy=HORIZON_SPECS[horizon]["bars_per_year_stock"] if market=="stock" else HORIZON_SPECS[horizon]["bars_per_year_crypto"]
        annual=0.08 if market=="stock" else 0.12
        charge=abs(cash)*annual/max(1,bpy)
        self.db.set_cash(aid,cash-charge)
        return charge

    def _margin_check(self, aid, market, horizon, ts):
        cash,gross,equity=self._account_marks(aid)
        if gross <= 0:
            return False
        maintenance=0.25 if market=="stock" else 0.20
        ratio=equity/gross
        if ratio >= maintenance:
            return False
        rate=self._cost_rate(market)
        marks=self.db.marks(aid)
        for pos in list(self.db.positions(aid)):
            px=float(marks.get(pos["symbol"],pos["avg_entry"]))
            fill=px*(1-rate); proceeds=float(pos["qty"])*fill
            acct=self.db.account(aid); self.db.set_cash(aid,float(acct["cash"])+proceeds)
            pnl=float(pos["qty"])*(fill-float(pos["avg_entry"])); ret=fill/float(pos["avg_entry"])-1
            self.db.add_trade({"account_id":aid,"symbol":pos["symbol"],"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,"strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":"MARGIN_LIQUIDATION","leverage":pos["leverage_at_entry"]})
            self.db.add_diagnostic(aid,pos["symbol"],pos["horizon"],ts.isoformat(),"LIQUIDATION","Maintenance margin breached",{"margin_ratio":ratio,"maintenance":maintenance,"pnl":pnl})
            self.db.delete_position(aid,pos["symbol"])
        return True

    def process_asset_horizon(self, market, symbol, horizon, now=None):
        aid=f"{market}_{horizon}"; pack=self.cache.ensure(market,symbol,horizon,now)
        df=self.cache.closed_only(pack["data"],market,horizon,now)
        spec=HORIZON_SPECS[horizon]
        if len(df)<spec["warmup"]: return {"processed":0,"reason":"insufficient_history","fetched":pack["fetched"],"api_called":pack.get("api_called",False)}
        model=self.db.model(market,symbol,horizon)
        if model is None:
            self.calibrate(market,symbol,horizon,now); model=self.db.model(market,symbol,horizon)
        last=self.db.last_processed(aid,symbol)
        eligible=df if last is None else df[df.index>pd.Timestamp(last)]
        # First run registers the latest closed bar as the forward starting point; it does not backfill trades.
        if last is None:
            self.db.set_last_processed(aid,symbol,df.index[-1].isoformat())
            return {"processed":0,"reason":"forward_registered","fetched":pack["fetched"],"api_called":pack.get("api_called",False)}
        if eligible.empty:return {"processed":0,"reason":"no_new_closed_bar","fetched":pack["fetched"],"api_called":pack.get("api_called",False)}
        processed=0
        for ts,row in eligible.iterrows():
            # At current OPEN we may only execute a decision that was saved after a PRIOR closed bar.
            self.db.set_mark(aid,symbol,ts.isoformat(),float(row.open))
            self._execute_pending(aid,market,symbol,ts,row)
            self._protect_position(aid,market,symbol,ts,row)
            financing=self._accrue_financing(aid,market,horizon)
            # Current decision is computed only after this bar has closed. It can execute next bar at earliest.
            self.db.set_mark(aid,symbol,ts.isoformat(),float(row.close))
            self._margin_check(aid,market,horizon,ts)
            hist=df.loc[:ts]
            cash,gross,equity=self._account_marks(aid)
            dec=decision_for(hist,market,horizon,model,max(equity,1.0)); dec["horizon"]=horizon
            dec.setdefault("diagnostics",{})["max_holding_bars"]=dec.pop("max_holding_bars")
            pos=self.db.position(aid,symbol)
            if pos is not None and dec["action"]=="EXIT" and not self.db.pending_order(aid,symbol):
                did=self.db.add_decision({"account_id":aid,"market":market,"symbol":symbol,"horizon":horizon,"bar_time":ts.isoformat(),**{k:v for k,v in dec.items() if k!="max_holding_bars"}})
                self.db.add_order({"account_id":aid,"symbol":symbol,"side":"SELL","created_bar":ts.isoformat(),"requested_notional":0.0,"qty":pos["qty"],"reason":"MODEL_EXIT","decision_id":did})
            elif pos is None and dec["action"]=="ENTER" and not self.db.pending_order(aid,symbol):
                did=self.db.add_decision({"account_id":aid,"market":market,"symbol":symbol,"horizon":horizon,"bar_time":ts.isoformat(),**{k:v for k,v in dec.items() if k!="max_holding_bars"}})
                self.db.add_order({"account_id":aid,"symbol":symbol,"side":"BUY","created_bar":ts.isoformat(),"requested_notional":dec["requested_notional"],"qty":None,"reason":"MODEL_ENTER","decision_id":did})
            else:
                self.db.add_decision({"account_id":aid,"market":market,"symbol":symbol,"horizon":horizon,"bar_time":ts.isoformat(),**{k:v for k,v in dec.items() if k!="max_holding_bars"}})
            cash,gross,equity=self._account_marks(aid)
            peak=self.db.peak_equity(aid) or float(self.db.account(aid)["initial_equity"]); peak=max(peak,equity); dd=equity/peak-1 if peak>0 else 0
            lev=gross/equity if equity>0 else float("inf")
            self.db.save_equity(aid,ts.isoformat(),equity,cash,gross,lev,dd)
            if financing>0:self.db.add_diagnostic(aid,symbol,horizon,ts.isoformat(),"FINANCING","Borrow cost charged",{"charge":financing})
            self.db.set_last_processed(aid,symbol,ts.isoformat()); processed+=1
        return {"processed":processed,"fetched":pack["fetched"],"api_called":pack.get("api_called",False)}

    def run_once(self, now=None):
        checked=0; processed=0; fetched=0; api_calls=0; errors=[]
        for a in self.db.assets():
            for hz in ("short","medium","long"):
                checked+=1
                try:
                    r=self.process_asset_horizon(a["market"],a["symbol"],hz,now); processed+=int(r.get("processed",0)); fetched+=int(r.get("fetched",0)); api_calls+=int(bool(r.get("api_called",False)))
                except Exception as e: errors.append({"market":a["market"],"symbol":a["symbol"],"horizon":hz,"error":f"{type(e).__name__}: {e}"})
        return {"status":"OK" if not errors else "PARTIAL","assets_checked":checked,"bars_processed":processed,"market_data_api_calls":api_calls,"api_rows_fetched":fetched,"broker_order_api_calls":0,"errors":errors}

    def account_summary(self):
        rows=[]
        for a in self.db.accounts():
            aid=a["account_id"]; eqrows=self.db.equity(aid); last=eqrows[-1] if eqrows else None
            equity=float(last["equity"]) if last else float(a["initial_equity"]); gross=float(last["gross_exposure"]) if last else 0.0
            rows.append({"account_id":aid,"market":a["market"],"horizon":a["horizon"],"initial_equity":a["initial_equity"],"equity":equity,"return_pct":equity/float(a["initial_equity"])-1,"cash":float(last["cash"]) if last else a["cash"],"gross_exposure":gross,"leverage":float(last["leverage"]) if last else 0.0,"drawdown":float(last["drawdown"]) if last else 0.0,"positions":len(self.db.positions(aid))})
        return rows