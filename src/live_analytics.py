from __future__ import annotations
import json
from collections import defaultdict
import numpy as np
import pandas as pd

HORIZON_LABELS = {"short":"短線","medium":"中線","long":"長線"}


def _safe_json(x):
    try:
        return json.loads(x or "{}") if isinstance(x, str) else (x or {})
    except Exception:
        return {}


def positions_table(db, cache=None):
    rows=[]
    for p in db.positions():
        aid=p["account_id"]; marks=db.marks(aid); mark=float(marks.get(p["symbol"],p["avg_entry"]))
        if cache is not None:
            market=aid.split("_",1)[0]
            tf="1Hour" if market=="stock" else "1h"
            live=cache.get(market,p["symbol"],tf)
            if live is not None and not live.empty:
                mark=float(live.close.iloc[-1])
        qty=float(p["qty"]); entry=float(p["avg_entry"]); notional=qty*mark
        pnl=qty*(mark-entry); ret=mark/entry-1 if entry else 0.0
        rows.append({
            "account_id":aid,"symbol":p["symbol"],"horizon":p.get("horizon"),"週期":HORIZON_LABELS.get(p.get("horizon"),p.get("horizon")),
            "strategy":p.get("strategy"),"entry_price":entry,"mark_price":mark,"qty":qty,"market_value":notional,
            "unrealized_pnl":pnl,"return_pct":ret,"stop_price":p.get("stop_price"),"target_price":p.get("target_price"),
            "leverage_at_entry":p.get("leverage_at_entry"),"bars_held":p.get("bars_held"),"regime_entry":p.get("regime_entry"),"entry_bar":p.get("entry_bar"),
        })
    return pd.DataFrame(rows)


def decisions_table(db, limit=300):
    rows=[]
    for d in db.recent_decisions(limit):
        diag=_safe_json(d.get("diagnostics_json"))
        rows.append({
            "bar_time":d.get("bar_time"),"account_id":d.get("account_id"),"market":d.get("market"),"symbol":d.get("symbol"),
            "horizon":d.get("horizon"),"週期":HORIZON_LABELS.get(d.get("horizon"),d.get("horizon")),"action":d.get("action"),
            "trade_confidence":float(d.get("confidence") or 0),"model_confidence":float(diag.get("model_confidence",diag.get("model_score",0)) or 0),
            "signal_strength":float(diag.get("signal_strength",0) or 0),"regime_score":float(diag.get("regime_score",diag.get("regime_fit",0)*100) or 0),
            "strategy":d.get("strategy"),"regime":d.get("regime"),"requested_notional":float(d.get("requested_notional") or 0),
            "leverage":float(d.get("leverage") or 0),"atr_pct":float(d.get("atr_pct") or 0),"reason":d.get("reason"),
        })
    return pd.DataFrame(rows)


def latest_by_asset_horizon(db):
    df=decisions_table(db,1000)
    if df.empty:return df
    df["_t"]=pd.to_datetime(df["bar_time"],utc=True,errors="coerce")
    return df.sort_values("_t",ascending=False).drop_duplicates(["market","symbol","horizon"]).drop(columns="_t")


def problem_ranking(db, min_samples=1):
    trades=pd.DataFrame(db.recent_trades(5000))
    if trades.empty:return pd.DataFrame()
    trades["realized_pnl"]=pd.to_numeric(trades["realized_pnl"],errors="coerce").fillna(0.0)
    trades["return_pct"]=pd.to_numeric(trades["return_pct"],errors="coerce").fillna(0.0)
    keys=["account_id","symbol","horizon","strategy","regime_entry"]
    out=[]
    for key,g in trades.groupby(keys,dropna=False):
        n=len(g)
        if n<min_samples:continue
        wins=g[g.realized_pnl>0].realized_pnl.sum(); losses=-g[g.realized_pnl<0].realized_pnl.sum()
        pf=float(wins/losses) if losses>0 else (float("inf") if wins>0 else np.nan)
        win_rate=float((g.realized_pnl>0).mean())
        avg_ret=float(g.return_pct.mean()); total=float(g.realized_pnl.sum())
        loss_rate=1-win_rate
        severity=(loss_rate*45)+max(0,-avg_ret*100)*4+max(0,-total)/500
        issue=[]
        if n>=3 and win_rate<0.40:issue.append("勝率偏低")
        if n>=3 and np.isfinite(pf) and pf<1:issue.append("Profit Factor<1")
        if avg_ret<0:issue.append("平均交易為負")
        if not issue:issue.append("樣本累積中" if n<5 else "暫無明顯問題")
        out.append({"account_id":key[0],"symbol":key[1],"horizon":key[2],"週期":HORIZON_LABELS.get(key[2],key[2]),"strategy":key[3],"regime":key[4],
                    "samples":n,"win_rate":win_rate,"profit_factor":pf,"avg_return":avg_ret,"realized_pnl":total,"severity":severity,"問題":" / ".join(issue)})
    return pd.DataFrame(out).sort_values(["severity","samples"],ascending=[False,False]) if out else pd.DataFrame()


def account_performance(db, lab):
    df=pd.DataFrame(lab.account_summary())
    if df.empty:return df
    stats=[]
    trades=pd.DataFrame(db.recent_trades(5000))
    for _,r in df.iterrows():
        aid=r["account_id"]; tr=trades[trades.account_id==aid] if not trades.empty else pd.DataFrame()
        if len(tr):
            wins=tr[tr.realized_pnl>0].realized_pnl.sum(); losses=-tr[tr.realized_pnl<0].realized_pnl.sum()
            pf=wins/losses if losses>0 else (float("inf") if wins>0 else np.nan)
            wr=float((tr.realized_pnl>0).mean())
        else: pf=np.nan; wr=np.nan
        stats.append({**r,"週期":HORIZON_LABELS.get(r["horizon"],r["horizon"]),"closed_trades":len(tr),"win_rate":wr,"profit_factor":pf})
    return pd.DataFrame(stats)


def latest_prices_table(db, cache):
    rows=[]
    for a in db.assets():
        market=a["market"]; symbol=a["symbol"]; tf="1Hour" if market=="stock" else "1h"
        df=cache.get(market,symbol,tf)
        if df is None or df.empty:continue
        ts=df.index[-1]; r=df.iloc[-1]
        prev=float(df.close.iloc[-2]) if len(df)>1 else float(r.close)
        rows.append({"market":market,"symbol":symbol,"price":float(r.close),"bar_time":ts.isoformat(),"change_pct":float(r.close/prev-1) if prev else 0.0,"volume":float(r.volume)})
    return pd.DataFrame(rows)
