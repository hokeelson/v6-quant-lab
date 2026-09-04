from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.crypto_lite_nav import render_crypto_lite_sidebar

from src.paths import db_path
from src.trial_ledger import TrialLedger
from src.ui_zh import horizon_label, market_label, strategy_label, translate_code

st.set_page_config(page_title="V6 模型實驗帳本", layout="wide")
render_crypto_lite_sidebar()
st.title("V6 模型實驗帳本（Trial Ledger）")
st.caption("記錄研究假設、重新校準、Champion / Challenger 治理與背景循環。此頁只做稽核，不會改變交易或模型。")

ledger = TrialLedger(db_path("trial_ledger.sqlite3"))
try:
    ledger.sync_governance(db_path("model_governance.sqlite3"))
except Exception as exc:
    st.warning(f"治理事件同步暫時失敗：{type(exc).__name__}: {exc}")

s = ledger.summary()
a, b, c, d, e, f = st.columns(6)
a.metric("研究試驗", s["research_trials"])
b.metric("不同模型假設", s["distinct_hypotheses"])
c.metric("挑戰者競賽", s["challenges"])
d.metric("升級事件", s["promotions"])
e.metric("帳本事件", s["events"])
f.metric("背景循環快照", s["cycles"])

if s["research_trials"] > 0:
    ratio = s["distinct_hypotheses"] / max(1, s["research_trials"])
    st.info(f"目前已留下 {s['research_trials']} 次研究紀錄、{s['distinct_hypotheses']} 個不同策略/參數指紋。Trial Ledger 會保留失敗與未升級的嘗試，避免只看最後贏家。")
else:
    st.info("帳本剛啟用。下一次研究／重新校準或治理事件發生後會開始累積紀錄。")

rows = pd.DataFrame(ledger.recent_events(500))
st.subheader("最近研究與治理事件")
if rows.empty:
    st.info("目前尚無事件。")
else:
    rows["時間"] = pd.to_datetime(rows["created_at"], utc=True, errors="coerce").dt.tz_convert("Asia/Taipei").dt.strftime("%Y-%m-%d %H:%M:%S")
    rows["事件"] = rows["event_type"].map(translate_code)
    rows["市場"] = rows["market"].map(lambda x: market_label(x) if pd.notna(x) else "—")
    rows["標的"] = rows["symbol"].fillna("—")
    rows["週期"] = rows["horizon"].map(lambda x: horizon_label(x) if pd.notna(x) else "—")
    rows["策略"] = rows["strategy"].map(lambda x: strategy_label(x) if pd.notna(x) else "—")
    rows["模型指紋"] = rows["model_signature"].fillna("—")
    rows["競賽ID"] = rows["arena_id"].fillna("—")
    rows["狀態"] = rows["status"].map(lambda x: translate_code(x) if pd.notna(x) else "—")
    st.dataframe(rows[["時間", "事件", "市場", "標的", "週期", "策略", "模型指紋", "競賽ID", "狀態"]], width="stretch", hide_index=True)

cycles = pd.DataFrame(ledger.recent_cycles(100))
st.subheader("背景循環稽核")
if cycles.empty:
    st.info("尚未留下背景循環快照。")
else:
    cycles["時間"] = pd.to_datetime(cycles["created_at"], utc=True, errors="coerce").dt.tz_convert("Asia/Taipei").dt.strftime("%Y-%m-%d %H:%M:%S")
    cycles["狀態"] = cycles["status"].map(translate_code)
    cycles = cycles.rename(columns={
        "assets_checked": "檢查組合", "bars_processed": "新K線", "true_errors": "真正錯誤",
        "data_quality_status": "資料品質", "data_quality_warnings": "資料警告",
        "data_quality_critical": "嚴重資料異常", "concept_drift_pairs": "Drift組數",
    })
    st.dataframe(cycles[["時間", "狀態", "檢查組合", "新K線", "真正錯誤", "資料品質", "資料警告", "嚴重資料異常", "Drift組數"]], width="stretch", hide_index=True)

with st.expander("查看事件原始內容"):
    if rows.empty:
        st.write("尚無資料")
    else:
        for _, r in rows.head(50).iterrows():
            try:
                payload = json.loads(r.get("payload_json") or "{}")
            except Exception:
                payload = r.get("payload_json")
            st.write(r.get("時間"), r.get("event_type"), payload)

st.caption("Trial Ledger 是防止多重測試偏誤的稽核基礎：未來 Deflated Sharpe／PBO 等修正可以直接使用『實際嘗試過的假設數』，而不是只使用最後留下的模型。")
