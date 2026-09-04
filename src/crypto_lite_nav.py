from __future__ import annotations

import streamlit as st


def render_crypto_lite_sidebar():
    with st.sidebar:
        st.page_link("dashboard_v9.py", label="主控台", icon="🏠")
        with st.expander("研究與稽核"):
            st.page_link("pages/08_Trial_Ledger.py", label="模型實驗帳本")
            st.page_link("pages/09_Expected_Live_Deviation.py", label="Live 偏差")
            st.page_link("pages/10_Risk_Sizing_Audit.py", label="風控稽核")
            st.page_link("pages/11_Crypto_V2_Shadow.py", label="Crypto V2 Shadow")
            st.page_link("pages/12_Crypto_V2_研究追蹤.py", label="Crypto V2 研究追蹤")
        st.caption("日常只需看主控台；研究頁不影響模擬交易。")
