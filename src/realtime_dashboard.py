from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from .paths import data_dir
from .realtime_layer import RealtimeDB
from .ui_zh import market_label, realtime_signal_label, status_label, translate_reason

STATUS_PATH = Path(data_dir()) / "realtime_status.json"

SOURCE_LABELS = {
    "BINANCE_STREAM": "Binance 即時串流",
    "ALPACA_IEX_STREAM": "Alpaca IEX 即時串流",
}


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


def _stream_label(value):
    return status_label(value)


@st.fragment(run_every="2s")
def render_realtime_panel():
    st.divider()
    st.subheader("秒級即時執行層")
    st.caption("秒級監控清單｜加密貨幣 Binance 即時串流＋美股 Alpaca IEX 即時串流｜目前為影子執行層，交易訂單介面呼叫 = 0")

    s = _status()
    if not s:
        st.warning("秒級背景程序尚未回報心跳；若剛部署請等待約 10～30 秒。")
        return
    age = _age_seconds(s.get("heartbeat_at"))
    fresh = age is not None and age <= 15
    raw_status = str(s.get("status") or "UNKNOWN").upper()
    if not fresh:
        display = "🔴 離線"
    elif raw_status == "DEGRADED":
        display = "🟠 部分異常"
    elif raw_status == "ERROR":
        display = "🔴 錯誤"
    else:
        display = "🟢 在線"

    cols = st.columns(7)
    cols[0].metric("即時層", display)
    cols[1].metric("心跳", f"{age} 秒" if age is not None else "—")
    cols[2].metric("監控標的", int(s.get("watchlist_total", 0) or 0))
    cols[3].metric("加密貨幣串流", _stream_label(s.get("crypto_stream", "—")))
    cols[4].metric("美股串流", _stream_label(s.get("stock_stream", "—")))
    cols[5].metric("台股", "僅K線")
    cols[6].metric("交易介面呼叫", "0")

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
                f"秒級監控清單異常｜背景程序讀到持倉 {positions_seen}、啟用標的 {assets_seen}、決策 {decisions_seen}｜"
                f"最後成功 {last_ok}｜錯誤：{err}"
            )
        else:
            st.info(
                f"秒級監控清單尚未建立｜背景程序目前讀到持倉 {positions_seen}、啟用標的 {assets_seen}、決策 {decisions_seen}｜"
                f"最後成功 {last_ok}"
            )

    if not quotes.empty:
        quotes["資料時間"] = quotes.ts.map(_fmt_ts)
        quotes["延遲秒"] = quotes.ts.map(_age_seconds)
        quotes["買賣價差"] = quotes.spread_bps.map(lambda x: "—" if pd.isna(x) else f"{x:.1f} 基點")
        quotes["市場中文"] = quotes.market.map(market_label)
        quotes["資料來源中文"] = quotes.source.map(lambda x: SOURCE_LABELS.get(str(x), str(x)))
        show = quotes.rename(columns={"symbol": "標的", "price": "即時價", "bid": "最佳買價", "ask": "最佳賣價"})
        st.markdown("**秒級行情**")
        st.dataframe(
            show[["資料時間", "延遲秒", "市場中文", "標的", "即時價", "最佳買價", "最佳賣價", "買賣價差", "資料來源中文"]].rename(
                columns={"市場中文": "市場", "資料來源中文": "資料來源"}
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("秒級行情尚未收到第一筆資料。加密貨幣通常會先出現；美股休市時沒有新成交屬正常。")

    if not signals.empty:
        signals["時間"] = signals.ts.map(_fmt_ts)
        signals["市場中文"] = signals.market.map(market_label)
        signals["即時狀態中文"] = signals.signal.map(realtime_signal_label)
        signals["說明中文"] = signals.detail.map(translate_reason)
        sig = signals.rename(columns={"symbol": "標的", "priority": "優先級", "confidence": "模型信心"})
        st.markdown("**即時執行監控**")
        st.dataframe(
            sig[["時間", "市場中文", "標的", "即時狀態中文", "優先級", "模型信心", "說明中文"]].rename(
                columns={"市場中文": "市場", "即時狀態中文": "即時狀態", "說明中文": "說明"}
            ),
            use_container_width=True,
            hide_index=True,
        )

    if not watch.empty:
        with st.expander("查看秒級監控清單"):
            watch["市場中文"] = watch.market.map(market_label)
            watch["入選原因中文"] = watch.reason.map(translate_reason)
            w = watch.rename(columns={"symbol": "標的", "score": "排名分數", "updated_at": "更新時間"})
            st.dataframe(
                w[["市場中文", "標的", "排名分數", "入選原因中文", "更新時間"]].rename(
                    columns={"市場中文": "市場", "入選原因中文": "入選原因"}
                ),
                use_container_width=True,
                hide_index=True,
            )

    st.caption("目前秒級層不會直接改變既有模擬成交規則；先累積影子資料驗證是否能改善進出場，再決定是否升級成正式執行條件。台股目前只使用K線資料，不會把延遲資料標示成假即時行情。")
