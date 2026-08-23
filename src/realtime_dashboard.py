from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from .paths import data_dir
from .realtime_layer import RealtimeDB
from .tca_engine import TCAStore
from .ui_zh import action_label, market_label, realtime_signal_label, status_label, translate_reason

STATUS_PATH = Path(data_dir()) / "realtime_status.json"
TCA_STATUS_PATH = Path(data_dir()) / "tca_status.json"

SOURCE_LABELS = {
    "BINANCE_STREAM": "Binance 即時串流",
    "ALPACA_IEX_STREAM": "Alpaca IEX 即時串流",
}

TRIGGER_LABELS = {
    "ENTRY_CONFIRM": "確認進場",
    "STOP_TOUCH": "觸及停損",
    "TARGET_TOUCH": "觸及目標",
}

TCA_STATE_LABELS = {
    "PENDING": "等待後續價格",
    "PARTIAL": "部分完成",
    "COMPLETE": "60秒完成",
}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _status():
    return _read_json(STATUS_PATH)


def _tca_status():
    return _read_json(TCA_STATUS_PATH)


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


def _bps(value):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):+.2f} 基點"
    except Exception:
        return "—"


def _price(value):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):.8g}"
    except Exception:
        return "—"


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
    tca = TCAStore(db)
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

    st.markdown("**秒級成交品質分析（TCA）**")
    tca_state = _tca_status()
    summary = tca.summary(500)
    tca_age = _age_seconds((tca_state or {}).get("heartbeat_at"))
    tca_online = tca_state is not None and tca_age is not None and tca_age <= 15 and str(tca_state.get("status")).upper() == "ONLINE"
    m = st.columns(7)
    m[0].metric("TCA程序", "🟢 在線" if tca_online else "⚪ 啟動中/等待")
    m[1].metric("TCA樣本", int(summary.get("samples", 0) or 0))
    m[2].metric("完成60秒", int(summary.get("complete_60s", 0) or 0))
    m[3].metric("平均執行成本", _bps(summary.get("avg_execution_cost_bps")))
    m[4].metric("平均買賣價差", _bps(summary.get("avg_spread_bps")))
    m[5].metric("平均60秒Markout", _bps(summary.get("avg_markout_60s_bps")))
    positive = summary.get("positive_60s_rate")
    m[6].metric("60秒有利率", "—" if positive is None else f"{float(positive) * 100:.1f}%")

    markout_cols = st.columns(3)
    markout_cols[0].metric("平均5秒Markout", _bps(summary.get("avg_markout_5s_bps")))
    markout_cols[1].metric("平均30秒Markout", _bps(summary.get("avg_markout_30s_bps")))
    markout_cols[2].metric("平均60秒Markout", _bps(summary.get("avg_markout_60s_bps")))

    events = pd.DataFrame(tca.events(100))
    if events.empty:
        st.info("尚未出現新的『確認進場／觸及停損／觸及目標』狀態切換。出現後會自動建立 TCA 樣本並追蹤 5／30／60 秒。")
    else:
        e = events.copy()
        e["時間"] = e.event_ts.map(_fmt_ts)
        e["市場中文"] = e.market.map(market_label)
        e["觸發中文"] = e.trigger_signal.map(lambda x: TRIGGER_LABELS.get(str(x), str(x)))
        e["方向中文"] = e.side.map(action_label)
        e["訊號價中文"] = e.signal_price.map(_price)
        e["影子成交價中文"] = e.shadow_fill_price.map(_price)
        e["執行成本中文"] = e.execution_cost_bps.map(_bps)
        e["買賣價差中文"] = e.spread_bps.map(_bps)
        e["5秒中文"] = e.markout_5s_bps.map(_bps)
        e["30秒中文"] = e.markout_30s_bps.map(_bps)
        e["60秒中文"] = e.markout_60s_bps.map(_bps)
        e["資料來源中文"] = e.source.map(lambda x: SOURCE_LABELS.get(str(x), str(x)))
        e["狀態中文"] = e.status.map(lambda x: TCA_STATE_LABELS.get(str(x), str(x)))
        st.dataframe(
            e[["時間", "市場中文", "symbol", "觸發中文", "方向中文", "訊號價中文", "影子成交價中文",
               "執行成本中文", "買賣價差中文", "5秒中文", "30秒中文", "60秒中文", "資料來源中文", "狀態中文"]].rename(columns={
                   "市場中文": "市場", "symbol": "標的", "觸發中文": "觸發", "方向中文": "方向",
                   "訊號價中文": "訊號價", "影子成交價中文": "影子成交價", "執行成本中文": "執行成本",
                   "買賣價差中文": "買賣價差", "5秒中文": "5秒Markout", "30秒中文": "30秒Markout",
                   "60秒中文": "60秒Markout", "資料來源中文": "資料來源", "狀態中文": "狀態",
               }),
            use_container_width=True,
            hide_index=True,
        )
    st.caption("TCA 為影子執行品質分析：買進用當下最佳賣價、賣出用當下最佳買價當作可執行影子成交價。Markout 正值代表成交後價格朝該交易方向移動；它不是整筆交易最終獲利率，也不會直接改變目前交易規則。")

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
