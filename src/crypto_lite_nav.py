from __future__ import annotations

import streamlit as st


def render_crypto_lite_sidebar():
    with st.sidebar:
        st.page_link("dashboard_v9.py", label="主控台", icon="🏠")
        with st.expander("研究與稽核"):
            st.page_link("pages/09_Expected_Live_Deviation.py", label="Live 偏差")
            st.page_link("pages/10_Risk_Sizing_Audit.py", label="風控稽核")
        st.caption("Crypto Lite 單一主線｜研究頁只保留目前帳戶直接相關的偏差與風控。")
