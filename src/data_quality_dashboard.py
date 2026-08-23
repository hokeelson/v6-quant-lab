from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .data_quality_drift import DataQualityDriftMonitor
from .paths import db_path
from .ui_zh import horizon_label, market_label

DATA_STATUS = {
    "OK": "🟢 正常",
    "WARNING": "🟠 警告",
    "CRITICAL": "🔴 嚴重",
    "UNKNOWN": "⚪ 未知",
}

DRIFT_STATUS = {
    "NORMAL": "🟢 正常",
    "LEARNING": "⚪ 累積證據",
    "WATCH": "🟡 觀察",
    "DRIFT": "🟠 市場漂移",
    "SEVERE": "🔴 嚴重漂移",
    "NO_MODEL": "⚪ 尚無模型",
}

TREND_LABEL = {
    "UP": "上升",
    "DOWN": "下降",
    "SIDEWAYS": "盤整",
    "UNKNOWN": "未知",
}


def _fmt_time(value):
    if not value:
        return "—"
    try:
        t = pd.Timestamp(value)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.tz_convert("Asia/Taipei").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _num(value, digits=2):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def _ratio(value):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):.2f}x"
    except Exception:
        return "—"


def _structural_count(payload: dict) -> int:
    s = payload.get("structural") or {}
    return int(sum(int(s.get(k, 0) or 0) for k in [
        "duplicates", "missing", "bad_high", "bad_low", "nonpositive_price",
        "negative_volume", "non_monotonic_time",
    ]))


def _data_note(payload: dict) -> str:
    notes = []
    structural = _structural_count(payload)
    if structural:
        notes.append(f"OHLCV結構異常 {structural}")
    stale = payload.get("stale_hours")
    limit = payload.get("stale_limit_hours")
    try:
        if stale is not None and limit is not None and float(stale) > float(limit):
            notes.append(f"行情過期 {float(stale):.1f} 小時")
    except Exception:
        pass
    extreme = int(payload.get("extreme_return_bars", 0) or 0)
    if extreme >= 4:
        notes.append(f"極端報酬 K 線 {extreme}")
    zero = payload.get("zero_volume_ratio")
    try:
        if zero is not None and float(zero) > 0.25:
            notes.append(f"零成交量比例 {float(zero) * 100:.1f}%")
    except Exception:
        pass
    return "、".join(notes) if notes else "無明顯資料異常"


def _drift_note(payload: dict) -> str:
    reasons = payload.get("drift_reasons") or payload.get("reasons") or []
    labels = {
        "volatility_shift": "波動率改變",
        "return_distribution_shift": "報酬分布改變",
        "atr_shift": "ATR 改變",
        "return_mean_shift": "平均報酬偏移",
        "trend_state_changed": "趨勢型態改變",
        "not_enough_post_calibration_evidence": "校準後樣本仍不足",
        "insufficient_history": "歷史資料不足",
        "no_model": "尚無模型",
    }
    return "、".join(labels.get(str(x), str(x)) for x in reasons) if reasons else "無明顯市場漂移"


def render_data_quality_panel():
    st.divider()
    st.subheader("資料品質＋市場結構漂移")
    st.caption(
        "OHLCV 結構／行情新鮮度與 Concept Drift 分開判斷｜Drift 以模型校準期為基準比較最新市場｜"
        "目前只調整虛擬進場部位，不直接刪除交易｜交易訂單介面呼叫 = 0"
    )

    try:
        monitor = DataQualityDriftMonitor(db_path("data_quality.sqlite3"))
        rows = monitor.latest_rows()
    except Exception as exc:
        st.error(f"資料品質監控讀取失敗：{type(exc).__name__}: {exc}")
        return

    if not rows:
        st.info("尚未完成第一輪資料品質掃描。背景程序下一個完整循環會自動建立資料。")
        return

    ok = sum(1 for r in rows if r.get("data_status") == "OK")
    warn = sum(1 for r in rows if r.get("data_status") == "WARNING")
    critical = sum(1 for r in rows if r.get("data_status") == "CRITICAL")
    drifted = sum(1 for r in rows if r.get("drift_status") in ("DRIFT", "SEVERE"))
    watch = sum(1 for r in rows if r.get("drift_status") == "WATCH")
    a, b, c, d, e = st.columns(5)
    a.metric("資料正常", ok)
    b.metric("資料警告", warn)
    c.metric("資料嚴重", critical)
    d.metric("Drift 觀察", watch)
    e.metric("明顯 Drift", drifted)

    table = []
    for r in rows:
        p = r.get("payload") or {}
        metrics = p.get("metrics") or {}
        table.append({
            "市場": market_label(r.get("market")),
            "標的": r.get("symbol"),
            "週期": horizon_label(r.get("horizon")),
            "資料狀態": DATA_STATUS.get(str(r.get("data_status")), str(r.get("data_status"))),
            "Drift狀態": DRIFT_STATUS.get(str(r.get("drift_status")), str(r.get("drift_status"))),
            "資料分數": _num(r.get("quality_score"), 1),
            "Drift分數": _num(r.get("drift_score"), 1),
            "進場倍率": f"{float(r.get('size_multiplier', 1.0) or 1.0):.2f}x",
            "最後K線": _fmt_time(r.get("last_bar")),
            "校準後K線": int(p.get("post_calibration_bars", 0) or 0),
            "波動倍率": _ratio(metrics.get("volatility_ratio")),
            "ATR倍率": _ratio(metrics.get("atr_ratio")),
            "KS差異": _num(metrics.get("ks_statistic"), 3),
            "基準趨勢": TREND_LABEL.get(str(metrics.get("baseline_trend")), str(metrics.get("baseline_trend") or "—")),
            "目前趨勢": TREND_LABEL.get(str(metrics.get("recent_trend")), str(metrics.get("recent_trend") or "—")),
            "資料原因": _data_note(p),
            "Drift原因": _drift_note(p),
        })

    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    st.caption(
        "Sizing 預設：正常 1.00x；輕度資料警告／Drift 觀察 0.85x；明顯 Drift 0.60x；"
        "嚴重資料品質或嚴重 Drift 0.40x。最後仍受整體最低部位倍率保護。"
    )

    try:
        events = monitor.recent_events(50)
    except Exception:
        events = []
    if events:
        with st.expander("最近資料品質／Drift 狀態變化"):
            e = pd.DataFrame(events)
            e["時間"] = e["created_at"].map(_fmt_time)
            e["市場"] = e["market"].map(market_label)
            e["週期"] = e["horizon"].map(horizon_label)
            e["標的"] = e["symbol"]
            e["原狀態"] = e["old_status"].fillna("—")
            e["新狀態"] = e["new_status"].fillna("—")
            st.dataframe(
                e[["時間", "市場", "標的", "週期", "原狀態", "新狀態"]],
                use_container_width=True,
                hide_index=True,
            )
