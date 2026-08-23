from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from .paths import data_dir
from .realtime_layer import RealtimeDB

STATUS_PATH = Path(data_dir()) / "realtime_status.json"


def _status():
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _age_seconds(ts):
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return max(0, int((pd.Timestamp.now(tz="UTC") - t).total_seconds()))
    except Exception:
        return None


def _fmt_ts(ts):
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.tz_convert("Asia/Taipei").strftime("%m-%d %H:%M:%S")
    except Exception:
        return "—"


@st.fragment(run_every="2s")
def render_realtime_panel():
    st.divider()
    st.subheader("Realtime Execution Layer")
    st.caption("秒級 Watchlist｜Crypto Binance Stream＋美股 Alpaca IEX Stream｜目前為 Shadow 執行層，交易訂單 API = 0")

    s = _status()
    if not s:
        st.warning("Realtime Worker 尚未回報心跳；若剛部署請等待約 10–30 秒。")
        return
    age = _age_seconds(s.get("heartbeat_at"))
    fresh = age is not None and age <= 15
    raw_status = str(s.get("status") or "UNKNOWN").upper()
    if not fresh:
        display = "🔴 OFFLINE"
    elif raw_status == "DEGRADED":
        display = "🟠 DEGRADED"
    elif raw_status == "ERROR":
        display = "🔴 ERROR"
    else:
        display = "🟢 ONLINE"

    cols = st.columns(7)
    cols[0].metric("Realtime", display)
    cols[1].metric("心跳", f"{age}s" if age is not None else "—")
    cols[2].metric("Watchlist", int(s.get("watchlist_total", 0) or 0))
    cols[3].metric("Crypto Stream", s.get("crypto_stream", "—"))
    cols[4].metric("美股 Stream", s.get("stock_stream", "—"))
    cols[5].metric("台股", "BAR_ONLY")
    cols[6].metric("交易 API", "0")

    db = RealtimeDB()
    quotes = pd.DataFrame(db.quotes())
    signals = pd.DataFrame(db.signals())
    watch = pd.DataFrame(db.watchlist())

    if raw_status in ("DEGRADED", "ERROR") or int(s.get("watchlist_total", 0) or 0) == 0:
        positions_seen = int(s.get("positions_seen", 0) or 0)
        assets_seen = int(s.get("assets_seen", 0) or 0)
        decisions_seen = int(s.get("decisions_seen", 0) or 0)
        last_ok = _fmt_ts(s.get("watchlist_last_success_at"))
        err = s.get("watchlist_last_error")
        if err:
            st.error(
                f"Realtime Watchlist 異常｜Realtime Worker 讀到持倉 {positions_seen}、ACTIVE 標的 {assets_seen}、決策 {decisions_seen}｜"
                f"最後成功 {last_ok}｜錯誤：{err}"
            )
        else:
            st.info(
                f"Realtime Watchlist 尚未建立｜Realtime Worker 目前讀到持倉 {positions_seen}、ACTIVE 標的 {assets_seen}、決策 {decisions_seen}｜"
                f"最後成功 {last_ok}"
            )

    if not quotes.empty:
        quotes["資料時間"] = quotes.ts.map(_fmt_ts)
        quotes["延遲秒"] = quotes.ts.map(_age_seconds)
        quotes["spread"] = quotes.spread_bps.map(lambda x: "—" if pd.isna(x) else f"{x:.1f} bps")
        show = quotes.rename(columns={"market": "市場", "symbol": "標的", "price": "即時價", "bid": "Bid", "ask": "Ask", "source": "來源"})
        st.markdown("**秒級行情**")
        st.dataframe(show[["資料時間", "延遲秒", "市場", "標的", "即時價", "Bid", "Ask", "spread", "來源"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("秒級行情尚未收到第一筆資料。Crypto 通常會先出現；美股休市時沒有新成交屬正常。")

    if not signals.empty:
        signals["時間"] = signals.ts.map(_fmt_ts)
        sig = signals.rename(columns={"market": "市場", "symbol": "標的", "signal": "即時狀態", "detail": "說明", "confidence": "模型信心"})
        st.markdown("**即時執行監控**")
        st.dataframe(sig[["時間", "市場", "標的", "即時狀態", "priority", "模型信心", "說明"]],
                     use_container_width=True, hide_index=True)

    if not watch.empty:
        with st.expander("查看秒級 Watchlist"):
            w = watch.rename(columns={"market": "市場", "symbol": "標的", "score": "排名分數", "reason": "入選原因", "updated_at": "更新時間"})
            st.dataframe(w[["市場", "標的", "排名分數", "入選原因", "更新時間"]], use_container_width=True, hide_index=True)

    st.caption("目前秒級層不會直接改變既有模擬成交規則；先累積 Shadow 資料驗證是否能改善進出場，再決定是否升級成正式執行條件。台股目前沒有被標示成假秒級資料。")
