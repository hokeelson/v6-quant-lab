from __future__ import annotations

import os
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

from src.migration_backup import build_migration_backup

load_dotenv()
st.set_page_config(page_title="Oracle Migration", layout="wide", page_icon="☁️")

_required_password = os.getenv("V6_DASHBOARD_PASSWORD", "") or os.getenv("V6_PASSWORD", "")
if _required_password and not st.session_state.get("v6_authenticated", False):
    st.warning("請先回到主 Dashboard 登入，再開啟此頁。")
    st.stop()

st.title("Oracle Migration")
st.caption("建立 Railway → Oracle 遷移備份。只包含 V6 SQLite 資料，不包含 API Key、Dashboard 密碼或其他環境變數。")

st.info("建議先讓 Railway 繼續運行。Oracle 驗證完成後再停 Railway，避免遷移期間中斷。")

if st.button("建立 V6 遷移備份", type="primary", use_container_width=True):
    with st.spinner("正在建立一致性 SQLite 備份..."):
        payload, manifest = build_migration_backup()
        st.session_state["oracle_migration_backup"] = payload
        st.session_state["oracle_migration_manifest"] = manifest

payload = st.session_state.get("oracle_migration_backup")
manifest = st.session_state.get("oracle_migration_manifest")
if payload and manifest:
    created = str(manifest.get("created_at") or "")
    names = manifest.get("databases") or []
    st.success(f"備份完成：{len(names)} 個 SQLite 資料庫；API Key / 密碼未包含。")
    st.write("包含：" + ("、".join(names) if names else "目前沒有 SQLite 資料庫"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "下載 Oracle 遷移備份",
        data=payload,
        file_name=f"v6_oracle_migration_{stamp}.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )

st.divider()
st.markdown("### 遷移順序")
st.markdown("1. 建立 Oracle Always Free VM。\n2. 在 Oracle 部署目前 GitHub 的 V6。\n3. 把本頁下載的 ZIP 還原到 Oracle 的 `/opt/v6-data`。\n4. 在 Oracle 重新設定 Alpaca API Key 與 Dashboard 密碼。\n5. 確認帳戶數、持倉、模型、Realtime 都正常後，最後才停 Railway。")
