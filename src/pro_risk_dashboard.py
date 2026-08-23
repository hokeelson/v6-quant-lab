from __future__ import annotations

import pandas as pd
import streamlit as st

from .pretrade_risk import read_pretrade_risk_snapshot
from .pro_risk_engine import read_professional_risk_snapshot

HORIZON_LABELS = {"short": "短線", "medium": "中線", "long": "長線"}
MARKET_LABELS = {"crypto": "Crypto", "stock": "美股", "twstock": "台股", "GLOBAL": "全系統"}
STATE_LABELS = {
    "LEARNING": "樣本累積中",
    "NORMAL": "正常",
    "WATCH": "觀察",
    "SHADOW_ONLY_CANDIDATE": "降為 Shadow 候選",
    "PAUSE_CANDIDATE": "暫停候選",
}
RISK_LABELS = {"LOW": "低", "MEDIUM": "中", "HIGH": "高", "CRITICAL": "極高"}
VERDICT_LABELS = {"ALLOW": "允許", "CAUTION": "注意", "BLOCK_CANDIDATE": "阻擋候選"}


def _pct(x):
    try:
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "—"


def _num(x, digits=2):
    try:
        return f"{float(x):,.{digits}f}"
    except Exception:
        return "—"


def _fmt_time(ts):
    if not ts:
        return "—"
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.tz_convert("Asia/Taipei").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


@st.fragment(run_every="30s")
def render_professional_risk_panel():
    snap = read_professional_risk_snapshot()
    pretrade_snap = read_pretrade_risk_snapshot()
    st.divider()
    st.subheader("Professional Risk Layer")
    st.caption("Portfolio Risk＋Pre-Trade Gate＋Strategy Health｜目前為 Shadow 風控層，不直接改變現有 Forward 成交｜交易訂單 API = 0")

    if not snap:
        st.info("尚未產生第一份專業風險快照。背景 Worker 下一個循環會自動建立。")
        return

    portfolio = snap.get("portfolio") or {}
    groups = pd.DataFrame(portfolio.get("groups") or [])
    positions = pd.DataFrame(portfolio.get("positions") or [])
    corr = pd.DataFrame(portfolio.get("correlations") or [])
    health = snap.get("strategy_health") or {}
    strategies = pd.DataFrame(health.get("strategies") or [])
    regimes = pd.DataFrame(health.get("regimes") or [])
    pretrade = pd.DataFrame((pretrade_snap or {}).get("candidates") or [])

    global_row = None
    if not groups.empty:
        m = groups[groups["group"] == "GLOBAL"]
        if not m.empty:
            global_row = m.iloc[0]

    a, b, c, d, e, f = st.columns(6)
    if global_row is not None:
        a.metric("整體風險", RISK_LABELS.get(global_row.get("risk_status"), global_row.get("risk_status", "—")))
        b.metric("Risk Score", f"{float(global_row.get('risk_score', 0)):.1f}/100")
        c.metric("總曝險", _pct(global_row.get("gross_ratio", 0)))
        d.metric("停損風險", _pct(global_row.get("stop_risk_pct", 0)))
        mc = float(global_row.get("max_pair_correlation", 0) or 0)
        e.metric("最高相關", f"{mc:.2f}")
        f.metric("Shadow倍率", f"{float(global_row.get('shadow_risk_multiplier', 1)):.2f}x")
    else:
        a.metric("整體風險", "低")
        b.metric("Risk Score", "0/100")
        c.metric("總曝險", "0.00%")
        d.metric("停損風險", "0.00%")
        e.metric("最高相關", "0.00")
        f.metric("Shadow倍率", "1.00x")

    if not groups.empty:
        g = groups.copy()
        g["市場"] = g["group"].map(lambda x: MARKET_LABELS.get(x, x))
        g["風險"] = g["risk_status"].map(lambda x: RISK_LABELS.get(x, x))
        g["總曝險"] = g["gross_ratio"].map(_pct)
        g["停損風險"] = g["stop_risk_pct"].map(_pct)
        g["最大單一權重"] = g["max_position_weight"].map(_pct)
        g["最高相關"] = g["max_pair_correlation"].map(lambda x: f"{float(x):.2f}")
        g["建議倍率"] = g["shadow_risk_multiplier"].map(lambda x: f"{float(x):.2f}x")
        st.markdown("**Portfolio Risk**")
        st.dataframe(
            g[["市場", "風險", "risk_score", "positions", "unique_symbols", "總曝險", "停損風險",
               "最大單一權重", "duplicate_symbols", "最高相關", "high_corr_pairs", "建議倍率"]].rename(columns={
                   "risk_score": "Risk Score", "positions": "持倉數", "unique_symbols": "不同標的",
                   "duplicate_symbols": "重複標的", "high_corr_pairs": "高相關組數",
               }),
            use_container_width=True,
            hide_index=True,
        )

    if not positions.empty:
        with st.expander("查看持倉風險拆解"):
            p = positions.copy()
            p["市場"] = p["market"].map(lambda x: MARKET_LABELS.get(x, x))
            p["週期"] = p["horizon"].map(lambda x: HORIZON_LABELS.get(x, x))
            p["名目部位"] = p["notional"].map(lambda x: _num(x, 2))
            p["停損風險額"] = p["stop_risk_amount"].map(lambda x: _num(x, 2))
            p["全系統權重"] = p["global_weight"].map(_pct)
            st.dataframe(
                p[["account_id", "市場", "symbol", "週期", "strategy", "mark_price", "名目部位",
                   "stop_price", "停損風險額", "全系統權重", "leverage"]],
                use_container_width=True,
                hide_index=True,
            )

    if not corr.empty:
        high = corr[corr["correlation"] >= 0.60].copy()
        if not high.empty:
            with st.expander("查看高相關持倉"):
                high["市場"] = high["market"].map(lambda x: MARKET_LABELS.get(x, x))
                high["相關係數"] = high["correlation"].map(lambda x: f"{float(x):.3f}")
                st.dataframe(high[["市場", "symbol_a", "symbol_b", "相關係數", "samples"]], use_container_width=True, hide_index=True)

    st.markdown("**Pre-Trade Risk Gate（Shadow）**")
    if pretrade_snap is None:
        st.info("Pre-Trade Gate 尚未產生第一份快照。")
    elif pretrade.empty:
        st.info("目前沒有最新 ENTER 候選需要做進場前組合風險檢查。")
    else:
        q = pretrade.copy()
        q["市場"] = q["market"].map(lambda x: MARKET_LABELS.get(x, x))
        q["週期"] = q["horizon"].map(lambda x: HORIZON_LABELS.get(x, x))
        q["判定"] = q["verdict"].map(lambda x: VERDICT_LABELS.get(x, x))
        q["Trade信心"] = q["trade_confidence"].map(lambda x: f"{float(x):.1f}")
        q["預計市場曝險"] = q["projected_gross_ratio"].map(_pct)
        q["最高相關"] = q["max_correlation"].map(lambda x: f"{float(x):.2f}")
        q["要求部位"] = q["requested_notional"].map(lambda x: _num(x, 2))
        q["Shadow部位倍率"] = q["shadow_size_multiplier"].map(lambda x: f"{float(x):.2f}x")
        st.dataframe(
            q[["市場", "symbol", "週期", "strategy", "Trade信心", "判定", "risk_score", "要求部位",
               "預計市場曝險", "duplicate_symbol", "most_correlated_symbol", "最高相關", "Shadow部位倍率", "flags"]].rename(columns={
                   "symbol": "標的", "strategy": "策略", "risk_score": "Risk Score",
                   "duplicate_symbol": "重複標的", "most_correlated_symbol": "最相關持倉", "flags": "風險原因",
               }),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("**Strategy Health**")
    if strategies.empty:
        st.info("已平倉樣本還不足，Strategy Health 會先保持樣本累積狀態。")
    else:
        s = strategies.copy()
        s["市場"] = s["market"].map(lambda x: MARKET_LABELS.get(x, x))
        s["週期"] = s["horizon"].map(lambda x: HORIZON_LABELS.get(x, x))
        s["狀態"] = s["state"].map(lambda x: STATE_LABELS.get(x, x))
        s["加權勝率"] = s["weighted_win_rate"].map(_pct)
        s["平均報酬"] = s["weighted_avg_return"].map(_pct)
        s["近期報酬"] = s["recent_avg_return"].map(_pct)
        s["惡化幅度"] = s["deterioration"].map(lambda x: "—" if pd.isna(x) else _pct(x))
        s["PF"] = s.apply(lambda r: "∞" if bool(r.get("profit_factor_infinite")) else ("—" if pd.isna(r.get("profit_factor")) else f"{float(r.get('profit_factor')):.2f}"), axis=1)
        s["Shadow權重"] = s["shadow_weight_multiplier"].map(lambda x: f"{float(x):.2f}x")
        st.dataframe(
            s[["市場", "週期", "strategy", "狀態", "samples", "health_score", "加權勝率", "PF",
               "平均報酬", "近期報酬", "惡化幅度", "max_loss_streak", "failure_votes", "Shadow權重"]].rename(columns={
                   "strategy": "策略", "samples": "樣本", "health_score": "Health Score",
                   "max_loss_streak": "最大連敗", "failure_votes": "失效警訊",
               }),
            use_container_width=True,
            hide_index=True,
        )

    if not regimes.empty:
        with st.expander("查看 Strategy × Regime 健康度"):
            r = regimes.copy()
            r["市場"] = r["market"].map(lambda x: MARKET_LABELS.get(x, x))
            r["週期"] = r["horizon"].map(lambda x: HORIZON_LABELS.get(x, x))
            r["狀態"] = r["state"].map(lambda x: STATE_LABELS.get(x, x))
            r["加權勝率"] = r["weighted_win_rate"].map(_pct)
            r["平均報酬"] = r["weighted_avg_return"].map(_pct)
            st.dataframe(
                r[["市場", "週期", "strategy", "regime", "狀態", "samples", "health_score",
                   "加權勝率", "平均報酬", "max_loss_streak"]].rename(columns={
                       "strategy": "策略", "regime": "Regime", "samples": "樣本",
                       "health_score": "Health Score", "max_loss_streak": "最大連敗",
                   }),
                use_container_width=True,
                hide_index=True,
            )

    st.caption(
        f"風險快照時間（台灣）：{_fmt_time(snap.get('generated_at'))}。"
        "Risk Score、Shadow 倍率與 Gate 判定都是透明的診斷規則，不是虧損機率；目前只監控，不直接縮倉、攔單或停用策略。"
    )
