from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from dotenv import load_dotenv

from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.live_analytics import (
    account_performance,
    decisions_table,
    latest_by_asset_horizon,
    latest_prices_table,
    positions_table,
    problem_ranking,
    trade_diagnostics_table,
)
from src.paths import data_dir
from src.twstock_support import TW_MARKET, normalize_tw_symbol
from src.ui_zh import (
    account_label,
    action_label,
    exit_reason_label,
    horizon_label,
    market_label,
    regime_label,
    strategy_label,
    translate_reason,
)

load_dotenv()
st.set_page_config(page_title="V6 即時模擬交易儀表板", layout="wide", page_icon="📊")

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
WORKER_STATUS_PATH = Path(data_dir()) / "worker_status.json"
WORKER_REQUEST_PATH = Path(data_dir()) / "worker_request.json"


def _queue_worker_request(kind: str) -> dict:
    payload = {
        "kind": kind,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard",
    }
    tmp = WORKER_REQUEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(WORKER_REQUEST_PATH)
    return payload


def _import_forward_candidates(uploaded) -> dict:
    if uploaded is None:
        return {"imported": 0, "active": 0}
    raw = uploaded.getvalue()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        con = sqlite3.connect(tmp_path)
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "candidates" not in tables:
            raise ValueError("這不是 V6 forward_validation.sqlite3：找不到 candidates 資料表")
        rows = [dict(r) for r in con.execute("SELECT * FROM candidates ORDER BY registered_at, candidate_id").fetchall()]
        con.close()
        before = {r["candidate_id"] for r in engine.forward.candidates()}
        for r in rows:
            try:
                params = json.loads(r.get("params_json") or "{}")
            except Exception:
                params = {}
            engine.forward.register_candidate({
                "candidate_id": r["candidate_id"], "market": r["market"], "symbol": r["symbol"],
                "strategy": r["strategy"], "params": params, "registered_at": r["registered_at"],
                "initial_capital": r["initial_capital"], "research_grade": r.get("research_grade"),
                "evidence_coverage": r.get("evidence_coverage"), "source_stage": r.get("source_stage", "stage3"),
                "status": r.get("status", "ACTIVE"), "notes": r.get("notes"),
            })
        after_rows = engine.forward.candidates()
        after = {r["candidate_id"] for r in after_rows}
        active = sum(1 for r in after_rows if r.get("status") == "ACTIVE")
        imported_assets = engine.import_active()
        return {"imported": len(after - before), "total_candidates": len(after), "active": active,
                "simulation_assets_added": imported_assets}
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def _read_worker_status():
    try:
        return json.loads(WORKER_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fmt_taipei(ts):
    if not ts:
        return "—"
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.tz_convert("Asia/Taipei").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _show_cycle_result(r: dict):
    sim = r.get("simulation", {}) or {}
    health = r.get("health", {}) or {}
    cal = r.get("calibration", {}) or {}
    total = int(health.get("total_pairs", 0))
    ready = int(health.get("ready_pairs", 0))
    waiting = int(health.get("waiting_history", len(cal.get("waiting_history", []) or [])))
    errors = int(health.get("true_errors", len(r.get("true_errors", []) or [])))
    a, b, c, d = st.columns(4)
    a.metric("可運行", f"{ready} / {total}" if total else "0")
    b.metric("等待更多資料", waiting)
    c.metric("真正錯誤", errors)
    d.metric("本次新K線", int(sim.get("bars_processed", 0)))
    if errors == 0:
        st.success(f"系統正常。{ready}/{total} 組可運行；等待歷史資料 {waiting} 組；交易介面呼叫 0 次。")
    else:
        st.error(f"有 {errors} 個真正錯誤需要處理。")
    waiting_rows = cal.get("waiting_history", []) or []
    true_errors = r.get("true_errors", []) or []
    if waiting_rows or true_errors:
        with st.expander("查看未就緒詳細資料"):
            if waiting_rows:
                w = pd.DataFrame(waiting_rows)
                if "market" in w.columns:
                    w["市場"] = w["market"].map(market_label)
                if "symbol" in w.columns:
                    w["標的"] = w["symbol"]
                if "horizon" in w.columns:
                    w["週期"] = w["horizon"].map(horizon_label)
                if "required_closed_bars" in w.columns:
                    w["最低完整K線"] = w["required_closed_bars"]
                cols = [c for c in ["市場", "標的", "週期", "最低完整K線"] if c in w.columns]
                st.markdown("**等待歷史資料**")
                st.dataframe(w[cols], width="stretch", hide_index=True)
            if true_errors:
                st.markdown("**真正錯誤**")
                st.dataframe(pd.DataFrame(true_errors), width="stretch", hide_index=True)


st.title("V6 即時模擬交易儀表板")
st.caption("加密貨幣＋美股＋台股｜9 個虛擬帳戶｜交易訂單介面呼叫 = 0｜SQLite 行情快取＋節流更新")

assets = db.assets()
market_counts = {m: sum(1 for a in assets if a.get("market") == m) for m in ("crypto", "stock", TW_MARKET)}
model_health = engine.model_health()
c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("虛擬帳戶", len(db.accounts()))
c2.metric("加密貨幣", market_counts["crypto"])
c3.metric("美股", market_counts["stock"])
c4.metric("台股", market_counts[TW_MARKET])
c5.metric("可運行組合", f"{model_health['ready_pairs']}/{model_health['total_pairs']}")
c6.metric("目前持倉", len(db.positions()))
c7.metric("交易訂單介面呼叫", "0")


@st.fragment(run_every="15s")
def worker_status_panel():
    s = _read_worker_status()
    st.subheader("自動背景程序狀態")
    if not s:
        st.warning("⚪ 尚未收到背景程序心跳。若剛重新部署，等待約 30～60 秒。")
        return
    try:
        hb = pd.Timestamp(s.get("heartbeat_at"))
        hb = hb.tz_localize("UTC") if hb.tzinfo is None else hb.tz_convert("UTC")
        age = max(0, int((pd.Timestamp.now(tz="UTC") - hb).total_seconds()))
    except Exception:
        age = 999999
    raw_status = str(s.get("status", "UNKNOWN")).upper()
    if age > 180:
        display, alert = "🔴 離線", "error"
    elif raw_status == "ERROR":
        display, alert = "🔴 錯誤", "error"
    elif raw_status == "DEGRADED":
        display, alert = "🟠 部分異常", "warning"
    elif raw_status == "RUNNING":
        display, alert = "🟢 執行中", "success"
    else:
        display, alert = "🟢 在線", "success"
    a, b, c, d, e, f, g = st.columns(7)
    a.metric("背景程序", display)
    b.metric("最後自動完成", _fmt_taipei(s.get("last_cycle_finished_at")))
    c.metric("心跳", f"{age} 秒前" if age < 999999 else "—")
    d.metric("本次檢查", int(s.get("assets_checked", 0) or 0))
    e.metric("本次新K線", int(s.get("bars_processed", 0) or 0))
    f.metric("行情介面呼叫", int(s.get("market_data_api_calls", 0) or 0))
    g.metric("交易介面呼叫", "0")
    msg = f"最後心跳（台灣）：{_fmt_taipei(s.get('heartbeat_at'))}｜背景程序每約 60 秒自動執行。"
    if alert == "success":
        st.success(msg)
    elif alert == "warning":
        st.warning(msg + f"｜真正錯誤：{int(s.get('true_errors', 0) or 0)}")
    else:
        detail = translate_reason(s.get("message", ""))
        st.error(msg + (f"｜{detail}" if detail != "—" else ""))


worker_status_panel()

if st.button("立即完整更新", type="primary", width="stretch"):
    payload = _queue_worker_request("full_cycle")
    st.session_state["manual_request_notice"] = payload

notice = st.session_state.pop("manual_request_notice", None)
if notice:
    st.success("立即完整更新已交給背景程序。儀表板不會同步重算，你可以繼續查看秒級執行與風控區塊。")


@st.fragment(run_every="30s")
def live_results():
    prices = latest_prices_table(db, engine.cache)
    st.subheader("最新行情（雲端快取）")
    if prices.empty:
        st.info("尚未建立行情快取。")
    else:
        px = prices.copy()
        px["時間"] = px["bar_time"]
        px["市場"] = px["market"].map(market_label)
        px["標的"] = px["symbol"]
        px["價格"] = px["price"]
        px["漲跌"] = px.change_pct.map(lambda x: f"{x * 100:+.2f}%")
        px["成交量"] = px["volume"]
        st.dataframe(px[["時間", "市場", "標的", "價格", "漲跌", "成交量"]], width="stretch", hide_index=True)

    acct = account_performance(db, lab)
    st.subheader("九個等本金帳戶")
    if not acct.empty:
        a = acct.copy()
        a["帳戶"] = a.account_id.map(account_label)
        a["市場中文"] = a.market.map(market_label)
        a["報酬率"] = a.return_pct.map(lambda x: f"{x * 100:.2f}%")
        a["回撤"] = a.drawdown.map(lambda x: f"{x * 100:.2f}%")
        a["勝率"] = a.win_rate.map(lambda x: "—" if pd.isna(x) else f"{x * 100:.1f}%")
        a["獲利因子"] = a.profit_factor.map(lambda x: "—" if pd.isna(x) else ("∞" if x == float("inf") else f"{x:.2f}"))
        a = a.rename(columns={
            "initial_equity": "初始本金", "equity": "帳戶淨值", "cash": "現金",
            "gross_exposure": "總曝險", "leverage": "槓桿", "positions": "持倉數",
            "closed_trades": "已平倉數",
        })
        st.dataframe(
            a[["帳戶", "市場中文", "週期", "初始本金", "帳戶淨值", "報酬率", "現金", "總曝險", "槓桿", "回撤",
               "持倉數", "已平倉數", "勝率", "獲利因子"]].rename(columns={"市場中文": "市場"}),
            width="stretch",
            hide_index=True,
        )

    pos = positions_table(db, engine.cache)
    st.subheader("目前持倉")
    if pos.empty:
        st.info("目前沒有持倉。")
    else:
        p = pos.copy()
        p["帳戶"] = p.account_id.map(account_label)
        p["標的"] = p.symbol
        p["策略"] = p.strategy.map(strategy_label)
        p["未實現報酬"] = p.return_pct.map(lambda x: f"{x * 100:.2f}%")
        p["未實現損益"] = p.unrealized_pnl.map(lambda x: f"{x:,.2f}")
        p = p.rename(columns={
            "entry_price": "進場價", "mark_price": "現價", "leverage_at_entry": "進場槓桿",
            "stop_price": "停損價", "target_price": "目標價", "bars_held": "持有K線數",
        })
        st.dataframe(
            p[["帳戶", "標的", "週期", "策略", "進場價", "現價", "未實現損益", "未實現報酬", "進場槓桿", "停損價", "目標價", "持有K線數"]],
            width="stretch",
            hide_index=True,
        )

    latest = latest_by_asset_horizon(db)
    st.subheader("最新短／中／長決策")
    if latest.empty:
        st.info("尚未產生決策。")
    else:
        v = latest.sort_values(["action", "trade_confidence"], ascending=[True, False]).copy()
        v["時間"] = v["bar_time"]
        v["市場中文"] = v.market.map(market_label)
        v["標的中文"] = v.symbol
        v["策略中文"] = v.strategy.map(strategy_label)
        v["動作"] = v.action.map(action_label)
        v["交易信心"] = v.trade_confidence.map(lambda x: f"{x:.1f}")
        v["模型信心"] = v.model_confidence.map(lambda x: f"{x:.1f}")
        v["訊號強度"] = v.signal_strength.map(lambda x: f"{x:.1f}")
        v["市場狀態"] = v.regime.map(regime_label)
        v["理由中文"] = v.reason.map(translate_reason)
        v = v.rename(columns={"requested_notional": "要求部位", "leverage": "槓桿"})
        st.dataframe(
            v[["時間", "市場中文", "標的中文", "週期", "策略中文", "動作", "交易信心", "模型信心", "訊號強度",
               "市場狀態", "要求部位", "槓桿", "理由中文"]].rename(columns={
                   "市場中文": "市場", "標的中文": "標的", "策略中文": "策略", "理由中文": "理由",
               }),
            width="stretch",
            hide_index=True,
        )

    problems = problem_ranking(db)
    st.subheader("策略問題排名")
    if problems.empty:
        st.info("已平倉樣本還太少。")
    else:
        q = problems.copy()
        q["標的"] = q.symbol
        q["策略中文"] = q.strategy.map(strategy_label)
        q["市場狀態"] = q.regime.map(regime_label)
        q["勝率"] = q.win_rate.map(lambda x: f"{x * 100:.1f}%")
        q["平均報酬"] = q.avg_return.map(lambda x: f"{x * 100:.2f}%")
        q["獲利因子"] = q.profit_factor.map(lambda x: "—" if pd.isna(x) else ("∞" if x == float("inf") else f"{x:.2f}"))
        q = q.rename(columns={"samples": "樣本數", "realized_pnl": "已實現損益"})
        st.dataframe(
            q[["標的", "週期", "策略中文", "市場狀態", "樣本數", "勝率", "獲利因子", "平均報酬", "已實現損益", "問題"]].rename(
                columns={"策略中文": "策略"}
            ).head(30),
            width="stretch",
            hide_index=True,
        )

    trades = trade_diagnostics_table(db, engine.cache, 100)
    st.subheader("最近已平倉＋問題診斷")
    if trades.empty:
        st.info("尚無已平倉交易。")
    else:
        t = trades.copy()
        t["平倉時間"] = t.exit_bar.map(_fmt_taipei)
        t["進場時間"] = t.entry_bar.map(_fmt_taipei)
        t["標的"] = t.symbol
        t["策略中文"] = t.strategy.map(strategy_label)
        t["損益"] = t.realized_pnl.map(lambda x: f"{x:,.2f}")
        t["報酬"] = t.return_pct.map(lambda x: f"{x * 100:+.2f}%")
        t["最大不利變動"] = t.mae.map(lambda x: "—" if pd.isna(x) else f"{x * 100:+.2f}%")
        t["最大有利變動"] = t.mfe.map(lambda x: "—" if pd.isna(x) else f"{x * 100:+.2f}%")
        t["進場信心"] = t.entry_confidence.map(lambda x: "—" if pd.isna(x) else f"{x:.1f}")
        t["進場市場狀態"] = t.regime_entry.map(regime_label)
        t["平倉原因中文"] = t.exit_reason.map(exit_reason_label)
        t["槓桿中文"] = t.leverage.map(lambda x: f"{x:.2f}x")
        t = t.rename(columns={"entry_price": "進場價", "exit_price": "出場價", "bars_held": "持有K線數"})
        cols = ["平倉時間", "標的", "週期", "策略中文", "進場時間", "進場價", "出場價", "持有K線數", "損益",
                "報酬", "最大不利變動", "最大有利變動", "進場信心", "進場市場狀態", "平倉原因中文", "問題診斷", "嚴重度", "槓桿中文"]
        st.dataframe(
            t[cols].rename(columns={"策略中文": "策略", "平倉原因中文": "平倉原因", "槓桿中文": "槓桿"}),
            width="stretch",
            hide_index=True,
        )

    dec = decisions_table(db, 500)
    if not dec.empty:
        last = pd.to_datetime(dec.bar_time, utc=True, errors="coerce").max()
        st.caption(f"最新決策資料時間：{last}｜儀表板只讀 SQLite；背景程序才補行情。")


live_results()

with st.expander("研究／維護工具"):
    st.markdown("### 台股")
    st.caption("台股短線＝1小時、中線＝1日、長線＝1週；虛擬交易採現股 1 倍，不會送券商訂單。")
    current_tw = [a["symbol"] for a in db.assets() if a.get("market") == TW_MARKET]
    st.write("目前台股標的：" + ("、".join(current_tw) if current_tw else "尚無"))
    tw_input = st.text_input("新增台股代號", placeholder="例如 2330、0050、6488.TWO")
    if st.button("加入台股模擬"):
        sym = normalize_tw_symbol(tw_input)
        if sym:
            lab.import_assets([{"market": TW_MARKET, "symbol": sym}])
            st.success(f"已加入 {sym}。背景程序會自動抓資料並校準短／中／長模型。")
            st.rerun()
        else:
            st.warning("請輸入台股代號。")

    st.divider()
    st.markdown("### 一次性匯入舊 V6 候選")
    uploaded_forward = st.file_uploader("選擇 forward_validation.sqlite3", type=["sqlite3", "db"], key="forward_db_upload")
    if uploaded_forward is not None and st.button("匯入啟用候選到雲端", type="primary"):
        try:
            result = _import_forward_candidates(uploaded_forward)
            st.success(f"匯入完成：新增 {result['imported']} 個候選；啟用 {result['active']} 個。")
            st.rerun()
        except Exception as e:
            st.error(f"匯入失敗：{type(e).__name__}: {e}")

    st.divider()
    if st.button("強制重新校準全部模型"):
        payload = _queue_worker_request("force_calibration")
        st.session_state["force_calibration_notice"] = payload

    force_notice = st.session_state.pop("force_calibration_notice", None)
    if force_notice:
        st.success("已交給背景程序分批強制校準；若策略／參數不同，會先進入 Challenger Forward 競賽，不會直接覆蓋目前冠軍。")

# 這三個區塊由同一個 Streamlit 入口直接繪製，避免重新執行時因模組快取而漏掉主畫面。
from src.realtime_dashboard import render_realtime_panel
from src.pro_risk_dashboard import render_professional_risk_panel
from src.governance_dashboard import render_governance_panel

render_realtime_panel()
render_professional_risk_panel()
render_governance_panel()
