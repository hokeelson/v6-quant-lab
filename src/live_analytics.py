from __future__ import annotations
import json
from collections import defaultdict
import numpy as np
import pandas as pd

from .market_cache import TIMEFRAME_MAP

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


def _trade_root_cause(ret, exit_reason, bars_held, mae, mfe, entry_confidence, leverage):
    ret=float(ret or 0.0)
    mae=float(mae) if pd.notna(mae) else np.nan
    mfe=float(mfe) if pd.notna(mfe) else np.nan
    bars=int(bars_held or 0)
    reason=str(exit_reason or "UNKNOWN").upper()
    conf=float(entry_confidence) if pd.notna(entry_confidence) else np.nan
    lev=float(leverage or 1.0)

    if ret >= 0:
        if reason == "ATR_TARGET":
            return "正常獲利：到達 ATR 目標", "—"
        return "正常獲利／模型退出", "—"

    severity = "高" if ret <= -0.05 else ("中" if ret <= -0.02 else "低")
    if lev >= 1.5 and ret <= -0.03:
        severity = "高"

    if reason == "MARGIN_LIQUIDATION":
        return "槓桿／風險過高導致強制平倉", "高"

    if reason == "ATR_STOP":
        if bars and bars <= 3 and (not np.isfinite(mfe) or mfe < 0.01):
            return "進場後快速反轉，疑似假突破／假訊號", "高"
        if np.isfinite(mfe) and mfe >= 0.03:
            return "曾有明顯浮盈但全部回吐至停損，鎖利偏慢", "高" if ret <= -0.03 else "中"
        return "訊號未延續，觸發保護性停損", severity

    if reason == "MODEL_EXIT":
        if bars and bars <= 3:
            return "訊號快速失效，可能進場過晚或假動能", "高" if ret <= -0.03 else "中"
        if np.isfinite(mfe) and mfe >= 0.03 and ret < 0:
            return "曾有浮盈但模型未及時退出，獲利回吐後轉虧", "高"
        if np.isfinite(mfe) and mfe < 0.01:
            text="進場後幾乎沒有正向延續，模型退出偏晚"
            if np.isfinite(conf) and conf >= 75:
                text += "；進場信心可能高估"
            return text, severity
        return "原策略訊號失效後模型退出", severity

    if reason == "TIME_EXIT":
        if not np.isfinite(mfe) or mfe < 0.01:
            return "持有期間缺乏趨勢，資金占用後時間退出", severity
        return "行情未能延續至目標，持有時間到期", severity

    return f"虧損平倉：{reason}", severity


def trade_diagnostics_table(db, cache, limit=100):
    """Explain closed trades using stored OHLCV only; no market-data API calls."""
    trades=pd.DataFrame(db.recent_trades(limit))
    if trades.empty:return pd.DataFrame()

    dec=decisions_table(db,5000)
    if not dec.empty:
        dec["_t"]=pd.to_datetime(dec["bar_time"],utc=True,errors="coerce")

    rows=[]
    for _,t in trades.iterrows():
        aid=str(t.get("account_id") or "")
        market=aid.split("_",1)[0] if "_" in aid else ""
        horizon=str(t.get("horizon") or (aid.split("_",1)[1] if "_" in aid else ""))
        symbol=str(t.get("symbol") or "").upper()
        entry=float(t.get("entry_price") or 0)
        exit_px=float(t.get("exit_price") or 0)
        ret=float(t.get("return_pct") or 0)
        entry_ts=pd.to_datetime(t.get("entry_bar"),utc=True,errors="coerce")
        exit_ts=pd.to_datetime(t.get("exit_bar"),utc=True,errors="coerce")

        bars=pd.DataFrame()
        if market in ("stock","crypto") and horizon in ("short","medium","long") and symbol and pd.notna(entry_ts) and pd.notna(exit_ts):
            try:
                alpaca_tf,binance_tf=TIMEFRAME_MAP[(market,horizon)]
                tf=alpaca_tf if market=="stock" else binance_tf
                bars=cache.get(market,symbol,tf,entry_ts,exit_ts)
            except Exception:
                bars=pd.DataFrame()

        if not bars.empty and entry>0:
            mae=float(bars.low.min()/entry-1)
            mfe=float(bars.high.max()/entry-1)
            bars_held=int(len(bars))
        else:
            mae=np.nan; mfe=np.nan; bars_held=0

        entry_conf=np.nan
        if not dec.empty and pd.notna(entry_ts):
            m=dec[(dec.account_id==aid)&(dec.symbol==symbol)&(dec.action=="ENTER")&(dec._t<entry_ts)]
            if not m.empty:
                entry_conf=float(m.sort_values("_t").iloc[-1].trade_confidence)

        issue,severity=_trade_root_cause(ret,t.get("exit_reason"),bars_held,mae,mfe,entry_conf,t.get("leverage"))
        rows.append({
            "exit_bar":t.get("exit_bar"),"entry_bar":t.get("entry_bar"),"account_id":aid,"symbol":symbol,
            "horizon":horizon,"週期":HORIZON_LABELS.get(horizon,horizon),"strategy":t.get("strategy"),
            "regime_entry":t.get("regime_entry"),"entry_price":entry,"exit_price":exit_px,
            "realized_pnl":float(t.get("realized_pnl") or 0),"return_pct":ret,"exit_reason":t.get("exit_reason"),
            "leverage":float(t.get("leverage") or 1),"bars_held":bars_held,"mae":mae,"mfe":mfe,
            "entry_confidence":entry_conf,"問題診斷":issue,"嚴重度":severity,
        })
    return pd.DataFrame(rows)


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
