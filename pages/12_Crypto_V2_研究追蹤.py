from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.crypto_v2.research import recent_blocked_candidates, recent_research_trades, research_summary
from src.crypto_v2.shadow_db import CryptoV2ShadowDB
from src.paths import db_path

st.set_page_config(page_title="Crypto V2 研究追蹤", layout="wide")
st.title("Crypto V2 研究追蹤")
st.caption("純觀測研究層：不改 V2 進場、出場、部位大小或組合風控，只記錄交易發生時的市場特徵與反事實結果。")

db = CryptoV2ShadowDB(db_path("crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
summary = research_summary(db)
exc = summary.get("trade_excursion_tracking") or {}
blocked = summary.get("risk_block_counterfactual") or {}
external = summary.get("external_signals") or {}

c1, c2, c3, c4 = st.columns(4)
c1.metric("已追蹤平倉", int(exc.get("tracked_closed_trades") or 0))
c2.metric("平均 MFE", "—" if exc.get("avg_mfe_pct") is None else f"{float(exc['avg_mfe_pct'])*100:.2f}%")
c3.metric("平均 MAE", "—" if exc.get("avg_mae_pct") is None else f"{float(exc['avg_mae_pct'])*100:.2f}%")
c4.metric("風控反事實已結束", int(blocked.get("closed_candidates") or 0))

st.caption("MFE = 進場後曾經到過的最大浮盈；MAE = 進場後曾經到過的最大浮虧。這兩個值用來研究停損、停利與持有時間，而不是直接改策略。")

st.subheader("組合風控到底有沒有擋對？")
r1, r2, r3, r4 = st.columns(4)
r1.metric("避免虧損候選", int(blocked.get("avoided_losses") or 0))
r2.metric("錯過獲利候選", int(blocked.get("missed_winners") or 0))
r3.metric(
    "擋單避損率",
    "—" if blocked.get("avoided_loss_rate") is None else f"{float(blocked['avoided_loss_rate'])*100:.1f}%",
)
r4.metric("被擋候選合計損益", f"{float(blocked.get('counterfactual_pnl') or 0.0):,.2f}")
st.caption("『被擋候選合計損益』是假設當時忽略組合風控、照原訊號進場的模擬結果；負值代表風控整體幫忙避開損失。這些候選完全不會扣模擬帳戶資金。")

st.subheader("交易時段績效")
session_name = {
    "ASIA": "亞洲時段",
    "EUROPE": "歐洲時段",
    "EU_US_OVERLAP": "歐美重疊時段",
    "US": "美國時段",
    "UNKNOWN": "未知",
}
rows = []
for session, data in (exc.get("by_session") or {}).items():
    rows.append({
        "時段": session_name.get(session, session),
        "平倉筆數": int(data.get("closed_trades") or 0),
        "勝率%": None if data.get("win_rate") is None else float(data["win_rate"]) * 100,
        "平均單筆報酬%": None if data.get("avg_return_pct") is None else float(data["avg_return_pct"]) * 100,
        "已實現損益": float(data.get("realized_pnl") or 0.0),
    })
if rows:
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
else:
    st.info("研究層剛啟用，需等這一版之後的新交易平倉才會開始累積時段統計。")

st.subheader("最近有研究資料的 V2 平倉")
trades = recent_research_trades(db, 100)
trade_rows = []
for t in trades:
    try:
        ctx = json.loads(t.get("context_json") or "{}")
    except Exception:
        ctx = {}
    trade_rows.append({
        "交易對": t.get("symbol"),
        "週期": t.get("horizon"),
        "進場時段": session_name.get(str(t.get("entry_session") or "UNKNOWN"), t.get("entry_session")),
        "進場市場": t.get("regime_entry"),
        "策略": t.get("strategy"),
        "單筆報酬%": float(t.get("return_pct") or 0.0) * 100,
        "MFE%": float(t.get("mfe_pct") or 0.0) * 100,
        "MAE%": float(t.get("mae_pct") or 0.0) * 100,
        "市場廣度%": None if ctx.get("breadth_above_ema20") is None else float(ctx["breadth_above_ema20"]) * 100,
        "平均相關性": ctx.get("avg_pairwise_correlation"),
        "BTC 24h%": None if ctx.get("btc_return_24h") is None else float(ctx["btc_return_24h"]) * 100,
        "出場原因": t.get("exit_reason"),
    })
if trade_rows:
    st.dataframe(pd.DataFrame(trade_rows), width="stretch", hide_index=True)
else:
    st.info("尚無研究層啟用後完成的 V2 平倉交易。舊交易不會被回填假造研究欄位。")

st.subheader("被組合風控擋掉的候選交易")
candidates = recent_blocked_candidates(db, 100)
candidate_rows = []
for r in candidates:
    candidate_rows.append({
        "交易對": r.get("symbol"),
        "週期": r.get("horizon"),
        "決策時間": r.get("decision_bar"),
        "狀態": r.get("status"),
        "策略": r.get("strategy"),
        "市場狀態": r.get("regime_entry"),
        "假設進場額": float(r.get("requested_notional") or 0.0),
        "假設報酬%": None if r.get("return_pct") is None else float(r["return_pct"]) * 100,
        "假設損益": r.get("simulated_pnl"),
        "MFE%": float(r.get("mfe_pct") or 0.0) * 100,
        "MAE%": float(r.get("mae_pct") or 0.0) * 100,
        "假設出場原因": r.get("exit_reason"),
    })
if candidate_rows:
    st.dataframe(pd.DataFrame(candidate_rows), width="stretch", hide_index=True)
else:
    st.info("目前還沒有研究層啟用後被組合風控擋掉的新候選。")

st.subheader("尚未接入的衍生品資料")
st.write({
    "Funding Rate": external.get("funding_rate"),
    "Open Interest": external.get("open_interest"),
    "Liquidations": external.get("liquidations"),
})
st.caption("這三項目前明確標示為 NOT_CONNECTED。之後若找到穩定且可驗證的來源，會先只做觀測與統計，不會直接餵給 V2 做交易決策。")
