from __future__ import annotations

import pandas as pd
import streamlit as st

from src.expected_live_deviation import expected_live_deviation_snapshot
from src.paths import db_path
from src.simulation_db import SimulationDB
from src.ui_zh import horizon_label, market_label, strategy_label


STATE_LABEL = {
    "LEARNING": "樣本累積中",
    "NORMAL": "正常",
    "WATCH": "注意",
    "DIVERGING": "明顯偏離",
    "SEVERE_DIVERGENCE": "嚴重偏離",
}
REASON_LABEL = {
    "OOS_POSITIVE_LIVE_NEGATIVE": "OOS 正報酬但前向轉負",
    "WIN_RATE_DETERIORATION": "勝率明顯惡化",
    "PROFIT_FACTOR_DETERIORATION": "獲利因子惡化",
    "EXPECTANCY_SIGN_REVERSAL": "單筆期望由正轉負",
    "LOSS_STREAK": "近期連敗過長",
}


def _pct(x):
    try:
        if pd.isna(x):
            return "—"
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "—"


def _num(x, digits=2):
    try:
        if pd.isna(x):
            return "—"
        if x == float("inf"):
            return "∞"
        return f"{float(x):.{digits}f}"
    except Exception:
        return "—"


st.set_page_config(page_title="Expected vs Live Deviation", layout="wide")
st.title("Expected vs Live Deviation")
st.caption(
    "比較模型校準時的 OOS 表現，與真正往前跑的虛擬已平倉交易。"
    "至少累積 5 筆後，偏離狀態才會影響未來虛擬進場部位；原始訊號與券商交易 API 不變。"
)

try:
    db = SimulationDB(db_path("simulation_lab.sqlite3"))
    snap = expected_live_deviation_snapshot(db)
except Exception as exc:
    st.error(f"Deviation 分析暫時無法讀取：{type(exc).__name__}: {exc}")
    st.stop()

summary = snap.get("summary") or {}
a, b, c, d, e, f = st.columns(6)
a.metric("模型數", int(summary.get("models", 0) or 0))
b.metric("已有前向交易", int(summary.get("with_live_trades", 0) or 0))
c.metric("注意", int(summary.get("watch", 0) or 0))
d.metric("明顯偏離", int(summary.get("diverging", 0) or 0))
e.metric("嚴重偏離", int(summary.get("severe", 0) or 0))
f.metric("Trading API", int(summary.get("broker_order_api_calls", 0) or 0))

if snap.get("active_sizing"):
    st.success("Active sizing 已啟用：至少 5 筆前向已平倉後，WATCH / DIVERGING / SEVERE_DIVERGENCE 會縮小未來虛擬進場部位。")
else:
    st.warning("Active sizing 目前停用；本頁只顯示偏離監控。")

rows = pd.DataFrame(snap.get("rows") or [])
if rows.empty:
    st.info("目前沒有可比較的模型資料。")
    st.stop()

view = rows.copy()
view["市場"] = view["market"].map(market_label)
view["週期"] = view["horizon"].map(horizon_label)
view["策略"] = view["strategy"].map(strategy_label)
view["狀態"] = view["state"].map(lambda x: STATE_LABEL.get(str(x), str(x)))
view["偏差分數"] = view["deviation_score"].map(lambda x: _num(x, 1))
view["Active倍率"] = view["suggested_confidence_multiplier"].map(lambda x: f"{float(x):.2f}x")
view["OOS勝率"] = view["oos_win_rate"].map(_pct)
view["前向勝率"] = view["live_win_rate"].map(_pct)
view["OOS總報酬"] = view["oos_total_return"].map(_pct)
view["前向複合報酬"] = view["live_compound_return"].map(_pct)
view["OOS單筆期望"] = view["oos_per_trade_return"].map(_pct)
view["前向單筆期望"] = view["live_per_trade_return"].map(_pct)
view["OOS獲利因子"] = view["oos_profit_factor"].map(_num)
view["前向獲利因子"] = view["live_profit_factor"].map(_num)
view["證據權重"] = view["evidence_weight"].map(_pct)
view["原因"] = view["reasons"].map(
    lambda xs: "、".join(REASON_LABEL.get(str(x), str(x)) for x in (xs or [])) or "—"
)

st.dataframe(
    view[[
        "市場", "symbol", "週期", "策略", "狀態", "偏差分數", "Active倍率",
        "live_closed_trades", "證據權重", "OOS勝率", "前向勝率", "OOS總報酬",
        "前向複合報酬", "OOS單筆期望", "前向單筆期望", "OOS獲利因子",
        "前向獲利因子", "live_max_loss_streak", "原因",
    ]].rename(columns={
        "symbol": "標的",
        "live_closed_trades": "前向已平倉",
        "live_max_loss_streak": "最大連敗",
    }),
    use_container_width=True,
    hide_index=True,
)

focus = rows[(rows["live_closed_trades"] >= 5) & (rows["state"] != "NORMAL") & (rows["state"] != "LEARNING")]
if not focus.empty:
    st.subheader("目前會影響 Active Sizing 的組合")
    for _, r in focus.head(10).iterrows():
        reasons = "、".join(REASON_LABEL.get(str(x), str(x)) for x in (r.get("reasons") or [])) or "無"
        st.write(
            f"{r['market']} / {r['symbol']} / {r['horizon']} / {r['strategy']}｜"
            f"{STATE_LABEL.get(str(r['state']), r['state'])}｜偏差 {float(r['deviation_score']):.1f}/100｜"
            f"倍率 {float(r['suggested_confidence_multiplier']):.2f}x｜樣本 {int(r['live_closed_trades'])}｜原因：{reasons}"
        )

st.info(
    "門檻固定為至少 5 筆前向已平倉：LEARNING=1.00x、NORMAL=1.00x、WATCH=0.85x、"
    "DIVERGING=0.65x、SEVERE_DIVERGENCE=0.40x。此層只影響未來虛擬進場大小，不改原始模型訊號。"
)
