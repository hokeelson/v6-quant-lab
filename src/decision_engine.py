from __future__ import annotations
import itertools, math
import numpy as np
import pandas as pd

from .backtest import ExecutionCosts, RiskRules, run_backtest
from .research import strategy_signal, robustness_score

HORIZON_SPECS = {
    "short": {"bars_per_year_stock":1638,"bars_per_year_crypto":8760,"warmup":300,"calibration":1200,"oos_frac":0.30,"min_trades":20,"risk_budget":0.005,"max_position":0.20,"max_leverage":2.0,"max_holding_stock":36,"max_holding_crypto":72,"atr_stop":1.7,"atr_target":2.8,"confidence":62},
    "medium":{"bars_per_year_stock":410,"bars_per_year_crypto":2190,"warmup":260,"calibration":1100,"oos_frac":0.30,"min_trades":12,"risk_budget":0.0075,"max_position":0.28,"max_leverage":2.0,"max_holding_stock":50,"max_holding_crypto":84,"atr_stop":2.2,"atr_target":4.5,"confidence":64},
    "long":  {"bars_per_year_stock":252,"bars_per_year_crypto":365,"warmup":260,"calibration":1200,"oos_frac":0.30,"min_trades":6,"risk_budget":0.010,"max_position":0.35,"max_leverage":1.5,"max_holding_stock":180,"max_holding_crypto":180,"atr_stop":2.8,"atr_target":7.0,"confidence":66},
}

PARAM_GRIDS = {
 "short": {
   "Trend MA":{"fast":[5,8,12,20],"slow":[20,30,40,60]},
   "Momentum":{"lookback":[6,12,24,48],"threshold":[0.0,0.01,0.02,0.04]},
   "Mean Reversion RSI":{"rsi_n":[5,7,10,14],"entry":[20,25,30,35],"exit_":[50,55,60]},
   "Breakout":{"lookback":[10,20,30,40],"exit_lookback":[5,10,15]},
 },
 "medium": {
   "Trend MA":{"fast":[8,12,20,30],"slow":[40,60,100,150]},
   "Momentum":{"lookback":[12,24,40,60],"threshold":[0.0,0.02,0.04,0.06]},
   "Mean Reversion RSI":{"rsi_n":[7,10,14,21],"entry":[20,25,30,35],"exit_":[50,55,60]},
   "Breakout":{"lookback":[20,40,60,90],"exit_lookback":[10,20,30]},
 },
 "long": {
   "Trend MA":{"fast":[10,20,30,50],"slow":[60,100,150,200]},
   "Momentum":{"lookback":[20,60,90,120],"threshold":[0.0,0.03,0.06,0.10]},
   "Mean Reversion RSI":{"rsi_n":[10,14,21],"entry":[20,25,30,35],"exit_":[50,55,60,65]},
   "Breakout":{"lookback":[40,55,80,120],"exit_lookback":[10,20,30,40]},
 }
}


def _grid(name,horizon):
    g=PARAM_GRIDS[horizon][name]; keys=list(g)
    for vals in itertools.product(*[g[k] for k in keys]):
        p=dict(zip(keys,vals))
        if name=="Trend MA" and p["fast"]>=p["slow"]: continue
        if name=="Breakout" and p["exit_lookback"]>=p["lookback"]: continue
        yield p


def atr(df,n=14):
    pc=df["close"].shift(1)
    tr=pd.concat([(df.high-df.low).abs(),(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=n).mean()


def market_regime(df):
    c=df.close
    if len(c)<80:return "UNKNOWN"
    a=atr(df,14)
    atrp=float((a.iloc[-1]/c.iloc[-1])) if c.iloc[-1]>0 and pd.notna(a.iloc[-1]) else np.nan
    fast=c.ewm(span=20,adjust=False).mean(); slow=c.ewm(span=60,adjust=False).mean()
    slope=float(slow.pct_change(10).iloc[-1]) if pd.notna(slow.pct_change(10).iloc[-1]) else 0.0
    vol=float(c.pct_change().rolling(40).std().iloc[-1])
    med=float(c.pct_change().rolling(40).std().rolling(120,min_periods=40).median().iloc[-1])
    high_vol=np.isfinite(vol) and np.isfinite(med) and med>0 and vol>1.35*med
    if fast.iloc[-1]>slow.iloc[-1] and slope>0.005: base="UP_TREND"
    elif fast.iloc[-1]<slow.iloc[-1] and slope<-0.005: base="DOWN_TREND"
    else: base="SIDEWAYS"
    return ("HIGH_VOL_" if high_vol else "NORMAL_")+base


def regime_fit(strategy,regime):
    if regime=="UNKNOWN": return 0.5
    up="UP_TREND" in regime; down="DOWN_TREND" in regime; side="SIDEWAYS" in regime; hv="HIGH_VOL" in regime
    if strategy=="Trend MA": v=0.95 if up else 0.30 if down else 0.55
    elif strategy=="Momentum": v=0.90 if up else 0.25 if down else 0.45
    elif strategy=="Mean Reversion RSI": v=0.90 if side and not hv else 0.45 if hv else 0.60
    else: v=0.85 if (up or hv) else 0.55
    return float(v)


def _costs_for(market):
    return ExecutionCosts(0,3,2) if market=="stock" else ExecutionCosts(10,5,4)


def calibrate_asset(df, market: str, horizon: str, initial_capital: float=100000.0):
    spec=HORIZON_SPECS[horizon]
    if len(df)<spec["warmup"]: raise ValueError(f"Need at least {spec['warmup']} closed bars")
    data=df.tail(spec["calibration"]).copy()
    split=max(spec["warmup"]//2,int(len(data)*(1-spec["oos_frac"])))
    if split>=len(data)-30: split=max(60,len(data)-max(30,int(len(data)*spec["oos_frac"])))
    train=data.iloc[:split]; test=data.iloc[split:]
    bpy=spec["bars_per_year_stock"] if market=="stock" else spec["bars_per_year_crypto"]
    costs=_costs_for(market)
    # calibration uses moderate fixed risk; forward broker will use ATR/risk-budget sizing.
    risk=RiskRules(max_position_pct=0.25,stop_loss_pct=0.12,take_profit_pct=0.30)
    regime=market_regime(data)
    best=None; rows=[]
    for strat in PARAM_GRIDS[horizon]:
        for p in _grid(strat,horizon):
            try:
                tr=run_backtest(train,strategy_signal(strat,train,p),initial_capital,costs,risk,bpy)
                te=run_backtest(test,strategy_signal(strat,test,p),initial_capital,costs,risk,bpy)
            except Exception:
                continue
            ts=robustness_score(tr["metrics"],spec["min_trades"]); os=robustness_score(te["metrics"],spec["min_trades"])
            rf=regime_fit(strat,regime)
            # penalize train/OOS divergence and thin OOS samples
            gap=abs(ts-os); stability=max(0.0,100-gap)
            sample=min(1.0,float(te["metrics"].get("closed_trades",0))/max(1,spec["min_trades"]))
            score=0.55*os+0.20*ts+0.10*stability+0.10*(rf*100)+0.05*(sample*100)
            row={"strategy":strat,"params":p,"train_score":ts,"oos_score":os,"regime_fit":rf,"stability":stability,"sample":sample,"score":score,"oos_metrics":te["metrics"]}
            rows.append(row)
            if best is None or score>best["score"]: best=row
    if best is None: raise RuntimeError("No strategy could be calibrated")
    ranked=sorted(rows,key=lambda x:x["score"],reverse=True)[:10]
    return {
      "strategy":best["strategy"],"params":best["params"],"calibration_score":float(best["score"]),
      "oos_score":float(best["oos_score"]),"train_score":float(best["train_score"]),"regime_fit":float(best["regime_fit"]),
      "calibrated_through":data.index[-1].isoformat(),
      "diagnostics":{"regime":regime,"stability":best["stability"],"sample":best["sample"],"oos_metrics":best["oos_metrics"],"top10":[{"strategy":r["strategy"],"params":r["params"],"score":r["score"],"oos_score":r["oos_score"]} for r in ranked]},
    }


def _rsi(close, n=14):
    d=close.diff(); up=d.clip(lower=0).rolling(n,min_periods=n).mean(); dn=(-d.clip(upper=0)).rolling(n,min_periods=n).mean()
    rs=up/dn.replace(0,np.nan)
    return 100-(100/(1+rs))


def _signal_strength(df, strategy, params, atr_pct):
    c=df.close; ap=max(float(atr_pct),1e-4)
    try:
        if strategy=="Trend MA":
            f=c.rolling(int(params["fast"])).mean().iloc[-1]; sl=c.rolling(int(params["slow"])).mean().iloc[-1]
            edge=(f/sl-1) if sl else 0.0
            return float(np.clip(50+50*edge/(2*ap),0,100))
        if strategy=="Momentum":
            lb=int(params["lookback"]); mom=float(c.pct_change(lb).iloc[-1]); th=float(params.get("threshold",0))
            return float(np.clip(50+50*(mom-th)/(3*ap*np.sqrt(max(lb,1))),0,100))
        if strategy=="Mean Reversion RSI":
            rv=float(_rsi(c,int(params["rsi_n"])).iloc[-1]); entry=float(params["entry"])
            if not np.isfinite(rv):return 0.0
            return float(np.clip(50+(entry-rv)*2.5,0,100))
        if strategy=="Breakout":
            lb=int(params["lookback"]); prior=float(c.shift(1).rolling(lb).max().iloc[-1]); px=float(c.iloc[-1])
            edge=(px/prior-1) if prior>0 else 0.0
            return float(np.clip(50+50*edge/(2*ap),0,100))
    except Exception:
        pass
    return 0.0


def decision_for(df, market: str, horizon: str, model: dict, equity: float):
    spec=HORIZON_SPECS[horizon]; strat=model["strategy"]; params=model["params"]
    sig=strategy_signal(strat,df,params)
    current=int(sig.iloc[-1]>0); prior=int(sig.iloc[-2]>0) if len(sig)>1 else 0
    reg=market_regime(df); rf=regime_fit(strat,reg)
    a=atr(df,14); ap=float(a.iloc[-1]/df.close.iloc[-1]) if pd.notna(a.iloc[-1]) and df.close.iloc[-1]>0 else 0.03
    stability=float(model.get("diagnostics",{}).get("stability",50)); sample=float(model.get("diagnostics",{}).get("sample",0.5))*100
    model_conf=float(np.clip(0.55*model["oos_score"]+0.20*model["train_score"]+0.15*stability+0.10*sample,0,100))
    signal_strength=_signal_strength(df,strat,params,ap)
    regime_score=float(rf*100)
    vol_quality=float(np.clip(100*(0.04/max(ap,0.005)),35,100))
    trade_conf=float(np.clip(0.45*model_conf+0.30*signal_strength+0.20*regime_score+0.05*vol_quality,0,100))
    stop=float(np.clip(spec["atr_stop"]*ap,0.01,0.30)); target=float(np.clip(spec["atr_target"]*ap,0.02,0.80))
    risk_budget=float(spec["risk_budget"]); raw_notional=float(equity*risk_budget/max(stop,1e-6)); base_cap=float(equity*spec["max_position"])
    conf_mult=float(np.clip((trade_conf-55)/30,0,1)); vol_guard=float(np.clip(0.04/max(ap,0.005),0.50,1.50))
    lev=1.0+(spec["max_leverage"]-1.0)*conf_mult*min(1.0,vol_guard)
    if "HIGH_VOL" in reg: lev=min(lev,1.15)
    lev=float(np.clip(lev,1.0,spec["max_leverage"])); notional=min(raw_notional,base_cap*lev)
    if current==0:
        action="EXIT" if prior==1 else "NO_TRADE"; reason="strategy_exit" if prior==1 else "no_active_signal"
    elif trade_conf<spec["confidence"]:
        action="NO_TRADE"; reason=f"trade_confidence_below_{spec['confidence']}"
    elif "DOWN_TREND" in reg and strat in ("Trend MA","Momentum"):
        action="NO_TRADE"; reason="regime_conflict"
    else:
        action="ENTER"; reason="qualified_signal"
    maxhold=spec["max_holding_stock"] if market=="stock" else spec["max_holding_crypto"]
    return {"action":action,"confidence":trade_conf,"strategy":strat,"params":params,"regime":reg,"atr_pct":ap,
            "stop_distance":stop,"target_distance":target,"risk_budget_pct":risk_budget,"requested_notional":float(notional),"leverage":lev,
            "max_holding_bars":int(maxhold),"reason":reason,
            "diagnostics":{"model_score":model["calibration_score"],"oos_score":model["oos_score"],"model_confidence":model_conf,
                           "signal_strength":signal_strength,"regime_score":regime_score,"regime_fit":rf,"vol_quality":vol_quality,
                           "raw_notional":raw_notional,"base_cap":base_cap,"vol_guard":vol_guard}}
