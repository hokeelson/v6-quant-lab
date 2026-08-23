from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .champion_challenger import ChampionChallenger
from .paths import db_path
from .ui_zh import horizon_label, market_label, strategy_label


STATUS_LABELS = {
    "ACTIVE": "競賽中",
    "PROMOTED": "挑戰成功・已升級",
    "REJECTED": "挑戰失敗・保留冠軍",
}

VERDICT_LABELS = {
    "WAITING": "等待第一批未來資料",
    "LEARNING": "累積 Forward 證據",
    "CONTINUE": "門檻未全過・繼續觀察",
    "PROMOTE_READY": "升級條件已通過",
    "REJECT_READY": "已達最長觀察期・不升級",
    "PROMOTED": "已升級為新冠軍",
    "REJECTED": "已淘汰挑戰者",
}

CHECK_LABELS = {
    "enough_days": "Forward 天數不足",
    "enough_closed_trades": "已平倉樣本不足",
    "positive_return": "挑戰者報酬尚未為正",
    "minimum_sharpe": "挑戰者 Sharpe 未達 0.5",
    "drawdown_floor": "挑戰者最大回撤超過 -25%",
    "beats_champion_return": "挑戰者報酬尚未明顯勝過冠軍",
    "beats_champion_sharpe": "挑戰者 Sharpe 尚未明顯勝過冠軍",
    "drawdown_not_materially_worse": "挑戰者回撤明顯比冠軍差",
    "paired_bootstrap_support": "同期間 paired bootstrap 支持度不足",
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


def _pct(value):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value) * 100:+.2f}%"
    except Exception:
        return "—"


def _num(value, digits=2):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def _prob(value):
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "—"


def _failed(value):
    if not value:
        return "目前沒有未通過項目"
    return "、".join(CHECK_LABELS.get(str(x), str(x)) for x in value)


def _params(value):
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value or "—")


@st.fragment(run_every="30s")
def render_governance_panel():
    st.divider()
    st.subheader("Champion / Challenger 模型治理")
    st.caption(
        "重新校準不直接覆蓋正式模型｜策略／參數改變後先做同期間 Forward Shadow 公平競賽｜"
        "只有證據完整勝過目前冠軍才升級｜交易訂單介面呼叫 = 0"
    )

    try:
        gov = ChampionChallenger(db_path("model_governance.sqlite3"))
        rows = gov.dashboard_rows(200)
    except Exception as exc:
        st.error(f"模型治理資料讀取失敗：{type(exc).__name__}: {exc}")
        return

    active = sum(1 for r in rows if str(r.get("status")) == "ACTIVE")
    promoted = sum(1 for r in rows if str(r.get("status")) == "PROMOTED")
    rejected = sum(1 for r in rows if str(r.get("status")) == "REJECTED")
    a, b, c, d, e = st.columns(5)
    a.metric("正在競賽", active)
    b.metric("歷史升級", promoted)
    c.metric("歷史淘汰", rejected)
    d.metric("最低 Forward", "60 天")
    e.metric("最低已平倉", "20 筆")

    if not rows:
        st.info(
            "目前尚未出現需要競賽的新策略／新參數。現有模型繼續當冠軍；"
            "下一次重新校準若產生不同策略或參數，系統會自動建立 Challenger，正式模型不會被直接覆蓋。"
        )
        st.caption(
            "同一策略＋同一參數只更新研究診斷，不會建立無意義的競賽。"
            "所有競賽都從註冊後的下一批完整 K 線開始，註冊前資料只可做指標暖機，不算 Forward 證據。"
        )
        return

    df = pd.DataFrame(rows)
    x = df.copy()
    x["市場"] = x["market"].map(market_label)
    x["週期"] = x["horizon"].map(horizon_label)
    x["狀態"] = x["status"].map(lambda v: STATUS_LABELS.get(str(v), str(v)))
    x["判定"] = x["verdict"].map(lambda v: VERDICT_LABELS.get(str(v), str(v)))
    x["開始時間"] = x["registered_at"].map(_fmt_time)
    x["冠軍策略"] = x["champion_strategy"].map(strategy_label)
    x["挑戰策略"] = x["challenger_strategy"].map(strategy_label)
    x["冠軍報酬"] = x["champion_return"].map(_pct)
    x["挑戰報酬"] = x["challenger_return"].map(_pct)
    x["冠軍Sharpe"] = x["champion_sharpe"].map(_num)
    x["挑戰Sharpe"] = x["challenger_sharpe"].map(_num)
    x["冠軍回撤"] = x["champion_max_drawdown"].map(_pct)
    x["挑戰回撤"] = x["challenger_max_drawdown"].map(_pct)
    x["同期間支持度"] = x["bootstrap_probability"].map(_prob)
    x["尚未通過"] = x["failed_checks"].map(_failed)

    st.dataframe(
        x[[
            "開始時間", "市場", "symbol", "週期", "狀態", "判定", "冠軍策略", "挑戰策略",
            "forward_days", "champion_closed_trades", "challenger_closed_trades",
            "冠軍報酬", "挑戰報酬", "冠軍Sharpe", "挑戰Sharpe",
            "冠軍回撤", "挑戰回撤", "同期間支持度", "尚未通過",
        ]].rename(columns={
            "symbol": "標的",
            "forward_days": "Forward天數",
            "champion_closed_trades": "冠軍已平倉",
            "challenger_closed_trades": "挑戰已平倉",
        }),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("查看冠軍／挑戰者凍結參數"):
        p = x.copy()
        p["冠軍參數"] = p["champion_params"].map(_params)
        p["挑戰參數"] = p["challenger_params"].map(_params)
        st.dataframe(
            p[["市場", "symbol", "週期", "冠軍策略", "冠軍參數", "挑戰策略", "挑戰參數", "開始時間"]]
            .rename(columns={"symbol": "標的"}),
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        "預設升級門檻：Forward ≥ 60 天、已平倉 ≥ 20 筆、挑戰者報酬 > 0、Sharpe ≥ 0.5、"
        "最大回撤 ≥ -25%；另外挑戰者報酬至少比冠軍高 1 個百分點、Sharpe 至少高 0.10、"
        "回撤不可明顯惡化，且 paired moving-block bootstrap 支持度至少 90%。"
    )
    st.caption(
        "競賽使用獨立 1x Shadow 資金與相同成本／風險規則，因此 Meta Model、組合風控和目前帳戶部位大小不會污染模型比較。"
    )
