from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st


def _pct_mult(x):
    try:
        return f"{float(x):.2f}x"
    except Exception:
        return "—"


def _money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "—"


def _num(x, digits=1):
    try:
        if pd.isna(x):
            return "—"
        return f"{float(x):.{digits}f}"
    except Exception:
        return "—"


st.set_page_config(page_title="Risk Sizing Audit", layout="wide")
st.title("Risk Sizing Audit")
st.caption(
    "逐筆查看虛擬 BUY 從原始部位到最終成交部位的風控縮放。"
    "包含 Portfolio、Strategy/Symbol Health、Expected-vs-Live、Meta、Data/Drift、槓桿硬上限與執行限制。"
)

path = Path("static") / "risk_sizing_audit.json"
try:
    snap = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    st.error(f"Sizing audit 暫時無法讀取：{type(exc).__name__}: {exc}")
    st.stop()

summary = snap.get("summary") or {}
a, b, c, d, e, f = st.columns(6)
a.metric("最近進場", int(summary.get("entries", 0) or 0))
b.metric("Expected-Live 縮倉", int(summary.get("expected_live_reduced", 0) or 0))
c.metric("Symbol Health 縮倉", int(summary.get("symbol_strategy_reduced", 0) or 0))
d.metric("Meta 縮倉", int(summary.get("meta_reduced", 0) or 0))
e.metric("槓桿 Guard", int(summary.get("leverage_guard_reduced", 0) or 0))
f.metric("Trading API", int(summary.get("broker_order_api_calls", 0) or 0))

rows = pd.DataFrame(snap.get("entries") or [])
if rows.empty:
    st.info("目前沒有 sizing audit 紀錄。")
    st.stop()

for col, default in [
    ("expected_live_multiplier", 1.0),
    ("expected_live_state", "LEARNING"),
    ("expected_live_samples", 0),
    ("expected_live_deviation_score", None),
    ("expected_live_reasons", []),
]:
    if col not in rows.columns:
        rows[col] = [default for _ in range(len(rows))]

view = rows.copy()
view["原始部位"] = view["original_notional"].map(_money)
view["Portfolio"] = view["portfolio_multiplier"].map(_pct_mult)
view["Health"] = view["effective_health_multiplier"].map(_pct_mult)
view["Expected-Live"] = view["expected_live_multiplier"].map(_pct_mult)
view["Expected狀態"] = view["expected_live_state"].fillna("LEARNING")
view["Expected樣本"] = view["expected_live_samples"].fillna(0).map(lambda x: int(x))
view["偏差分數"] = view["expected_live_deviation_score"].map(lambda x: _num(x, 1))
view["Meta"] = view["meta_multiplier"].map(_pct_mult)
view["Data/Drift"] = view["quality_drift_multiplier"].map(_pct_mult)
view["綜合倍率"] = view["combined_multiplier"].map(_pct_mult)
view["Leverage Guard"] = view["leverage_guard_multiplier"].map(_pct_mult)
view["執行限制"] = view["execution_cap_multiplier"].map(_pct_mult)
view["最終倍率"] = view["final_effective_multiplier"].map(_pct_mult)
view["最終部位"] = view["filled_notional"].map(_money)
view["Expected原因"] = view["expected_live_reasons"].map(
    lambda xs: ", ".join(str(x) for x in (xs or [])) or "—"
)

show_cols = [
    "created_at", "market", "symbol", "horizon", "strategy", "原始部位",
    "Portfolio", "Health", "Expected-Live", "Expected狀態", "Expected樣本", "偏差分數",
    "Meta", "Data/Drift", "綜合倍率", "Leverage Guard", "執行限制", "最終倍率", "最終部位",
    "Expected原因",
]
show_cols = [c for c in show_cols if c in view.columns]
st.dataframe(view[show_cols], width="stretch", hide_index=True)

floor_rows = rows[rows.get("final_effective_multiplier", pd.Series(dtype=float)) <= 0.250001]
if not floor_rows.empty:
    st.warning(
        f"最近 {len(rows)} 筆中有 {len(floor_rows)} 筆最終倍率落在約 0.25x 底線。"
        "這是後續檢查 double penalty 的重點。"
    )

st.info("此頁只顯示虛擬交易風控診斷；不會呼叫券商交易 API。")
