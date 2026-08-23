from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .paths import db_path
from .pretrade_risk import read_pretrade_risk_snapshot
from .pro_risk_engine import read_professional_risk_snapshot
from .simulation_db import SimulationDB
from .ui_zh import (
    account_label,
    bool_label,
    health_label,
    horizon_label,
    market_label,
    regime_label,
    risk_label,
    strategy_label,
    translate_code,
    verdict_label,
)


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


def _flags(value):
    if isinstance(value, (list, tuple, set)):
        return "、".join(translate_code(x) for x in value) if value else "—"
    return translate_code(value)


def _recent_active_sizing(limit=100):
    try:
        db = SimulationDB(db_path("simulation_lab.sqlite3"))
        rows = []
        for d in db.diagnostics(max(200, limit * 3)):
            if str(d.get("category") or "") != "RISK_SIZING":
                continue
            try:
                payload = json.loads(d.get("payload_json") or "{}")
            except Exception:
                payload = {}
            rows.append({**d, **payload})
            if len(rows) >= limit:
                break
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


@st.fragment(run_every="30s")
def render_professional_risk_panel():
    snap = read_professional_risk_snapshot()
    pretrade_snap = read_pretrade_risk_snapshot()
    st.divider()
    st.subheader("專業風控層")
    st.caption("組合風險＋進場前風險閘門＋策略健康度｜已開始調整虛擬進場部位大小，不直接封鎖交易｜交易訂單介面呼叫 = 0")

    if not snap:
        st.info("尚未產生第一份專業風險快照。背景程序下一個循環會自動建立。")
        return

    portfolio = snap.get("portfolio") or {}
    groups = pd.DataFrame(portfolio.get("groups") or [])
    positions = pd.DataFrame(portfolio.get("positions") or [])
    corr = pd.DataFrame(portfolio.get("correlations") or [])
    health = snap.get("strategy_health") or {}
    strategies = pd.DataFrame(health.get("strategies") or [])
    regimes = pd.DataFrame(health.get("regimes") or [])
    pretrade = pd.DataFrame((pretrade_snap or {}).get("candidates") or [])
    active_sizing = _recent_active_sizing(100)

    global_row = None
    if not groups.empty:
        m = groups[groups["group"] == "GLOBAL"]
        if not m.empty:
            global_row = m.iloc[0]

    a, b, c, d, e, f = st.columns(6)
    if global_row is not None:
        a.metric("整體風險", risk_label(global_row.get("risk_status")))
        b.metric("風險分數", f"{float(global_row.get('risk_score', 0)):.1f}/100")
        c.metric("總曝險", _pct(global_row.get("gross_ratio", 0)))
        d.metric("停損風險", _pct(global_row.get("stop_risk_pct", 0)))
        mc = float(global_row.get("max_pair_correlation", 0) or 0)
        e.metric("最高相關", f"{mc:.2f}")
        f.metric("目前建議倍率", f"{float(global_row.get('shadow_risk_multiplier', 1)):.2f}x")
    else:
        a.metric("整體風險", "低")
        b.metric("風險分數", "0/100")
        c.metric("總曝險", "0.00%")
        d.metric("停損風險", "0.00%")
        e.metric("最高相關", "0.00")
        f.metric("目前建議倍率", "1.00x")

    st.markdown("**實際虛擬部位調整紀錄**")
    if active_sizing.empty:
        st.info("新版風控尚未遇到下一筆實際進場成交。新 BUY 成交後會在這裡顯示原始部位與風控後部位。")
    else:
        z = active_sizing.copy()
        z["時間"] = z["bar_time"].map(_fmt_time)
        z["帳戶"] = z["account_id"].map(account_label)
        z["週期"] = z["horizon"].map(horizon_label)
        z["原始要求部位"] = z["original_notional"].map(lambda x: _num(x, 2))
        z["風控後目標部位"] = z["adjusted_notional"].map(lambda x: _num(x, 2))
        z["實際成交部位"] = z["filled_notional"].map(lambda x: _num(x, 2))
        z["總倍率"] = z["combined_multiplier"].map(lambda x: f"{float(x):.2f}x")
        z["組合倍率"] = z["portfolio_multiplier"].map(lambda x: f"{float(x):.2f}x")
        z["策略倍率"] = z["strategy_multiplier"].map(lambda x: f"{float(x):.2f}x")
        z["策略狀態"] = z["strategy_state"].map(health_label)
        z["進場風險判定"] = z["pretrade_verdict"].map(verdict_label)
        z["風險原因"] = z["flags"].map(_flags)
        st.dataframe(
            z[["時間", "帳戶", "symbol", "週期", "原始要求部位", "風控後目標部位", "實際成交部位",
               "總倍率", "組合倍率", "策略倍率", "策略狀態", "進場風險判定", "風險原因"]].rename(columns={"symbol": "標的"}),
            use_container_width=True,
            hide_index=True,
        )

    if not groups.empty:
        g = groups.copy()
        g["市場"] = g["group"].map(market_label)
        g["風險"] = g["risk_status"].map(risk_label)
        g["總曝險"] = g["gross_ratio"].map(_pct)
        g["停損風險"] = g["stop_risk_pct"].map(_pct)
        g["最大單一權重"] = g["max_position_weight"].map(_pct)
        g["最高相關"] = g["max_pair_correlation"].map(lambda x: f"{float(x):.2f}")
        g["建議倍率"] = g["shadow_risk_multiplier"].map(lambda x: f"{float(x):.2f}x")
        st.markdown("**組合風險**")
        st.dataframe(
            g[["市場", "風險", "risk_score", "positions", "unique_symbols", "總曝險", "停損風險",
               "最大單一權重", "duplicate_symbols", "最高相關", "high_corr_pairs", "建議倍率"]].rename(columns={
                   "risk_score": "風險分數", "positions": "持倉數", "unique_symbols": "不同標的",
                   "duplicate_symbols": "重複標的數", "high_corr_pairs": "高相關組數",
               }),
            use_container_width=True,
            hide_index=True,
        )

    if not positions.empty:
        with st.expander("查看持倉風險拆解"):
            p = positions.copy()
            p["帳戶"] = p["account_id"].map(account_label)
            p["市場"] = p["market"].map(market_label)
            p["週期"] = p["horizon"].map(horizon_label)
            p["策略"] = p["strategy"].map(strategy_label)
            p["名目部位"] = p["notional"].map(lambda x: _num(x, 2))
            p["停損風險額"] = p["stop_risk_amount"].map(lambda x: _num(x, 2))
            p["全系統權重"] = p["global_weight"].map(_pct)
            st.dataframe(
                p[["帳戶", "市場", "symbol", "週期", "策略", "mark_price", "名目部位",
                   "stop_price", "停損風險額", "全系統權重", "leverage"]].rename(columns={
                       "symbol": "標的", "mark_price": "現價", "stop_price": "停損價", "leverage": "槓桿",
                   }),
                use_container_width=True,
                hide_index=True,
            )

    if not corr.empty:
        high = corr[corr["correlation"] >= 0.60].copy()
        if not high.empty:
            with st.expander("查看高相關持倉"):
                high["市場"] = high["market"].map(market_label)
                high["相關係數"] = high["correlation"].map(lambda x: f"{float(x):.3f}")
                st.dataframe(
                    high[["市場", "symbol_a", "symbol_b", "相關係數", "samples"]].rename(columns={
                        "symbol_a": "標的一", "symbol_b": "標的二", "samples": "樣本數",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

    st.markdown("**進場前風險閘門**")
    if pretrade_snap is None:
        st.info("進場前風險閘門尚未產生第一份快照。")
    elif pretrade.empty:
        st.info("目前沒有最新進場候選需要做進場前組合風險檢查。")
    else:
        q = pretrade.copy()
        q["市場"] = q["market"].map(market_label)
        q["週期"] = q["horizon"].map(horizon_label)
        q["策略"] = q["strategy"].map(strategy_label)
        q["判定"] = q["verdict"].map(verdict_label)
        q["交易信心"] = q["trade_confidence"].map(lambda x: f"{float(x):.1f}")
        q["預計市場曝險"] = q["projected_gross_ratio"].map(_pct)
        q["最高相關"] = q["max_correlation"].map(lambda x: f"{float(x):.2f}")
        q["要求部位"] = q["requested_notional"].map(lambda x: _num(x, 2))
        q["建議部位倍率"] = q["shadow_size_multiplier"].map(lambda x: f"{float(x):.2f}x")
        q["重複標的中文"] = q["duplicate_symbol"].map(bool_label)
        q["風險原因中文"] = q["flags"].map(_flags)
        st.dataframe(
            q[["市場", "symbol", "週期", "策略", "交易信心", "判定", "risk_score", "要求部位",
               "預計市場曝險", "重複標的中文", "most_correlated_symbol", "最高相關", "建議部位倍率", "風險原因中文"]].rename(columns={
                   "symbol": "標的", "risk_score": "風險分數", "重複標的中文": "重複標的",
                   "most_correlated_symbol": "最相關持倉", "風險原因中文": "風險原因",
               }),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("**策略健康度**")
    if strategies.empty:
        st.info("已平倉樣本還不足，策略健康度會先保持樣本累積狀態。")
    else:
        s = strategies.copy()
        s["市場"] = s["market"].map(market_label)
        s["週期"] = s["horizon"].map(horizon_label)
        s["策略"] = s["strategy"].map(strategy_label)
        s["狀態"] = s["state"].map(health_label)
        s["加權勝率"] = s["weighted_win_rate"].map(_pct)
        s["平均報酬"] = s["weighted_avg_return"].map(_pct)
        s["近期報酬"] = s["recent_avg_return"].map(_pct)
        s["惡化幅度"] = s["deterioration"].map(lambda x: "—" if pd.isna(x) else _pct(x))
        s["獲利因子"] = s.apply(lambda r: "∞" if bool(r.get("profit_factor_infinite")) else ("—" if pd.isna(r.get("profit_factor")) else f"{float(r.get('profit_factor')):.2f}"), axis=1)
        s["部位權重"] = s["shadow_weight_multiplier"].map(lambda x: f"{float(x):.2f}x")
        st.dataframe(
            s[["市場", "週期", "策略", "狀態", "samples", "health_score", "加權勝率", "獲利因子",
               "平均報酬", "近期報酬", "惡化幅度", "max_loss_streak", "failure_votes", "部位權重"]].rename(columns={
                   "samples": "樣本數", "health_score": "健康分數",
                   "max_loss_streak": "最大連敗", "failure_votes": "失效警訊",
               }),
            use_container_width=True,
            hide_index=True,
        )

    if not regimes.empty:
        with st.expander("查看策略 × 市場狀態健康度"):
            r = regimes.copy()
            r["市場"] = r["market"].map(market_label)
            r["週期"] = r["horizon"].map(horizon_label)
            r["策略"] = r["strategy"].map(strategy_label)
            r["市場狀態"] = r["regime"].map(regime_label)
            r["狀態"] = r["state"].map(health_label)
            r["加權勝率"] = r["weighted_win_rate"].map(_pct)
            r["平均報酬"] = r["weighted_avg_return"].map(_pct)
            st.dataframe(
                r[["市場", "週期", "策略", "市場狀態", "狀態", "samples", "health_score",
                   "加權勝率", "平均報酬", "max_loss_streak"]].rename(columns={
                       "samples": "樣本數", "health_score": "健康分數", "max_loss_streak": "最大連敗",
                   }),
                use_container_width=True,
                hide_index=True,
            )

    st.caption(
        f"風險快照時間（台灣）：{_fmt_time(snap.get('generated_at'))}。"
        "風險分數與健康度仍是透明診斷規則，不是虧損機率。新版只會縮放虛擬進場部位，不會直接封鎖訊號、強制停用策略或呼叫券商交易 API。"
    )
