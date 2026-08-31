from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from dotenv import load_dotenv

from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.live_analytics import account_performance, positions_table
from src.ui_zh import account_label, horizon_label, market_label

load_dotenv()
st.set_page_config(page_title="V6 決策中心", layout="wide", page_icon="📊")

_required_password = os.getenv("V6_DASHBOARD_PASSWORD", "") or os.getenv("V6_PASSWORD", "")
if _required_password and not st.session_state.get("v6_authenticated", False):
    st.title("V6 網頁量化研究室")
    with st.form("v6_login"):
        _pw = st.text_input("密碼", type="password")
        _ok = st.form_submit_button("登入", type="primary")
    if _ok:
        if _pw == _required_password:
            st.session_state["v6_authenticated"] = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    st.stop()

with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}

engine = AutoOrchestratorV8(float((cfg.get("research") or {}).get("initial_capital", 100000)))
db = engine.db
lab = engine.lab


def _load_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _pct(v) -> str:
    try:
        return f"{float(v) * 100:+.2f}%"
    except Exception:
        return "—"


def _direction_zh(v: str) -> str:
    return {"LONG": "偏多", "SHORT": "偏空", "NO_TRADE": "暫不交易"}.get(str(v), "資料不足")


def _risk_zh(v: str) -> str:
    return {
        "NORMAL": "正常",
        "CAUTION": "注意",
        "RISK_OFF": "偏高",
        "RISK_OFF_HIGH": "很高",
        "UNAVAILABLE": "資料不足",
    }.get(str(v), str(v or "資料不足"))


def _market_name(v: str) -> str:
    return {"crypto": "加密貨幣", "stock": "美股", "twstock": "台股"}.get(str(v), str(v))


def _pick_market_direction(rows: list[dict], market: str) -> tuple[str, int, int, int]:
    subset = [r for r in rows if str(r.get("market")) == market]
    long_n = sum(1 for r in subset if r.get("direction") == "LONG")
    short_n = sum(1 for r in subset if r.get("direction") == "SHORT")
    no_n = sum(1 for r in subset if r.get("direction") == "NO_TRADE")
    if not subset:
        return "NO_DATA", 0, 0, 0
    if no_n >= max(long_n, short_n) and no_n >= len(subset) * 0.45:
        return "NO_TRADE", long_n, short_n, no_n
    if long_n > short_n * 1.15:
        return "LONG", long_n, short_n, no_n
    if short_n > long_n * 1.15:
        return "SHORT", long_n, short_n, no_n
    return "NO_TRADE", long_n, short_n, no_n


def _direction_stats(research: dict) -> dict:
    shadow = research.get("direction_shadow") or {}
    stats = shadow.get("decision_stats") or shadow.get("by_decision") or {}
    return stats if isinstance(stats, dict) else {}


def _system_conclusion(direction_rows: list[dict], research: dict) -> str:
    crypto_dir, _, _, _ = _pick_market_direction(direction_rows, "crypto")
    stock_dir, _, _, _ = _pick_market_direction(direction_rows, "stock")
    stats = _direction_stats(research)
    long_avg = ((stats.get("LONG") or {}).get("avg_forward_return_pct"))
    short_avg = ((stats.get("SHORT") or {}).get("avg_forward_return_pct"))
    parts = [f"目前加密貨幣 {_direction_zh(crypto_dir)}、美股 {_direction_zh(stock_dir)}。"]
    if long_avg is not None and short_avg is not None:
        if float(short_avg) > float(long_avg):
            parts.append("目前 Forward 證據顯示 SHORT 優於 LONG。")
        elif float(long_avg) > float(short_avg):
            parts.append("目前 Forward 證據顯示 LONG 優於 SHORT。")
    parts.append("如果 Long / Short 的 Forward 平均報酬都仍為負，優先少做交易、等待高 EV 機會。")
    return " ".join(parts)


@st.fragment(run_every="30s")
def decision_center():
    direction = _load_json(Path("static") / "direction_shadow_snapshot.json")
    external = _load_json(Path("static") / "daily_external_intelligence.json")
    research = _load_json(Path("static") / "research_snapshot.json")
    health = _load_json(Path("static") / "runtime_health.json")

    rows = direction.get("rows") or []
    ext_markets = external.get("markets") or {}

    st.title("V6 決策中心")
    st.caption("平常只看這一頁即可；完整研究細節保留在左側研究頁面。Paper / Shadow only。")

    overall = str(health.get("overall_status") or "UNKNOWN")
    broker_calls = ((health.get("safety") or {}).get("broker_order_api_calls"))
    if overall == "HEALTHY" and broker_calls == 0:
        st.success("系統運作正常｜Paper / Shadow 模式｜Broker order API calls = 0")
    elif health:
        st.warning(f"系統狀態：{overall}｜Broker order API calls：{broker_calls if broker_calls is not None else '—'}")
    else:
        st.info("正在等待最新 runtime health。")

    st.subheader("現在市場怎麼看")
    cols = st.columns(3)
    for col, market in zip(cols, ("crypto", "stock", "twstock")):
        direction_name, long_n, short_n, no_n = _pick_market_direction(rows, market)
        ext = ext_markets.get(market) or {}
        with col:
            st.metric(_market_name(market), _direction_zh(direction_name))
            st.caption(f"多 {long_n}｜空 {short_n}｜不做 {no_n}｜外部風險：{_risk_zh(ext.get('risk_regime'))}")

    st.subheader("系統現在的中文結論")
    st.info(_system_conclusion(rows, research))

    st.subheader("今天最值得先看的標的")
    qualified = [
        r for r in rows
        if r.get("direction") in ("LONG", "SHORT")
        and float(r.get("direction_confidence") or 0.0) >= 0.55
        and float(r.get("ev_gap_r") or 0.0) >= 0.08
    ]
    qualified.sort(key=lambda r: (float(r.get("direction_confidence") or 0.0), float(r.get("ev_gap_r") or 0.0)), reverse=True)
    top = qualified[:12]
    if not top:
        st.info("目前沒有方向與 EV 差距同時足夠明確的候選，暫時以 NO_TRADE 為主。")
    else:
        df = pd.DataFrame([{
            "市場": _market_name(r.get("market")),
            "標的": r.get("symbol"),
            "週期": horizon_label(r.get("horizon")),
            "方向": "做多" if r.get("direction") == "LONG" else "做空",
            "信心": f"{float(r.get('direction_confidence') or 0.0) * 100:.1f}%",
            "Long EV": f"{float(r.get('long_ev_r') or 0.0):+.2f}R",
            "Short EV": f"{float(r.get('short_ev_r') or 0.0):+.2f}R",
            "EV差距": f"{float(r.get('ev_gap_r') or 0.0):.2f}R",
            "市場狀態": r.get("regime"),
        } for r in top])
        st.dataframe(df, width="stretch", hide_index=True)

    st.subheader("Long / Short 最近哪邊比較有效")
    stats = _direction_stats(research)
    metric_cols = st.columns(3)
    for col, key, label in zip(metric_cols, ("LONG", "SHORT", "NO_TRADE"), ("LONG 做多", "SHORT 做空", "NO_TRADE 不做")):
        row = stats.get(key) or {}
        with col:
            if row:
                avg = row.get("avg_forward_return_pct")
                if avg is None:
                    avg = row.get("avg_forward_reward_pct")
                hit = row.get("hit_rate")
                if hit is None:
                    hit = row.get("hit_rate_pct")
                    if hit is not None and float(hit) > 1:
                        hit = float(hit) / 100.0
                evaluated = row.get("evaluated")
                if evaluated is None:
                    evaluated = row.get("completed")
                col.metric(label, _pct(avg))
                col.caption(f"完成 {int(evaluated or 0)} 筆｜命中率 {float(hit or 0.0) * 100:.1f}%")
            else:
                col.metric(label, "等待資料")

    if stats:
        def _avg(key):
            row = stats.get(key) or {}
            v = row.get("avg_forward_return_pct")
            if v is None:
                v = row.get("avg_forward_reward_pct")
            return float(v or 0.0)
        long_avg = _avg("LONG")
        short_avg = _avg("SHORT")
        no_row = stats.get("NO_TRADE") or {}
        no_hit = no_row.get("hit_rate")
        if no_hit is None:
            no_hit = no_row.get("hit_rate_pct")
            if no_hit is not None and float(no_hit) > 1:
                no_hit = float(no_hit) / 100.0
        no_hit = float(no_hit or 0.0)
        if long_avg < 0 and short_avg < 0:
            st.warning(f"目前 LONG 與 SHORT 的 Forward 平均報酬都還是負值；NO_TRADE 命中率約 {no_hit * 100:.1f}%。目前應偏向少做、挑高 EV 機會。")

    st.subheader("帳戶總覽")
    acct = account_performance(db, lab)
    if not acct.empty:
        a = acct.copy()
        a["帳戶"] = a.account_id.map(account_label)
        a["市場"] = a.market.map(market_label)
        a["週期"] = a.account_id.map(lambda x: horizon_label(str(x).rsplit("_", 1)[-1]))
        a["淨值"] = a.equity.map(lambda x: f"{float(x):,.0f}")
        a["報酬"] = a.return_pct.map(lambda x: f"{float(x) * 100:+.2f}%")
        a["槓桿"] = a.leverage.map(lambda x: f"{float(x):.2f}x")
        a["現金"] = a.cash.map(lambda x: f"{float(x):,.0f}")
        st.dataframe(a[["帳戶", "市場", "週期", "淨值", "報酬", "槓桿", "現金"]], width="stretch", hide_index=True)

    pos = positions_table(db, engine.cache)
    with st.expander("目前持倉（需要時再看）"):
        if pos.empty:
            st.info("目前沒有持倉。")
        else:
            p = pos.copy()
            cols = [c for c in ["account_id", "symbol", "strategy", "return_pct", "unrealized_pnl"] if c in p.columns]
            st.dataframe(p[cols], width="stretch", hide_index=True)

    st.caption("MFE / MAE / EV proxy、Expected Live、Risk Sizing、Crypto V2 研究追蹤等完整診斷仍保留在左側研究頁面。")


decision_center()
