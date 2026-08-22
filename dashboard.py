from __future__ import annotations
import json, yaml
import os
import sqlite3
import tempfile
from pathlib import Path
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from src.auto_orchestrator import AutoOrchestrator
from src.live_analytics import account_performance, positions_table, decisions_table, latest_by_asset_horizon, problem_ranking, latest_prices_table

load_dotenv(); st.set_page_config(page_title="V6 Live Dashboard",layout="wide",page_icon="📊")

# Accept the new Railway variable name and the legacy local name.
_required_password = os.getenv("V6_DASHBOARD_PASSWORD", "") or os.getenv("V6_PASSWORD", "")
if _required_password and not st.session_state.get("v6_authenticated", False):
    st.title("V6 Web Quant Lab")
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
with open("config.yaml","r",encoding="utf-8") as f: cfg=yaml.safe_load(f)
engine=AutoOrchestrator(float(cfg["research"]["initial_capital"])); db=engine.db; lab=engine.lab


def _import_forward_candidates(uploaded) -> dict:
    """Merge Stage 4 candidates from a local forward_validation.sqlite3 into cloud DB.
    Existing cloud candidates are kept; no local file replaces the live DB.
    """
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
                "candidate_id": r["candidate_id"],
                "market": r["market"],
                "symbol": r["symbol"],
                "strategy": r["strategy"],
                "params": params,
                "registered_at": r["registered_at"],
                "initial_capital": r["initial_capital"],
                "research_grade": r.get("research_grade"),
                "evidence_coverage": r.get("evidence_coverage"),
                "source_stage": r.get("source_stage", "stage3"),
                "status": r.get("status", "ACTIVE"),
                "notes": r.get("notes"),
            })
        after_rows = engine.forward.candidates()
        after = {r["candidate_id"] for r in after_rows}
        active = sum(1 for r in after_rows if r.get("status") == "ACTIVE")
        imported_assets = engine.import_active()
        return {"imported": len(after-before), "total_candidates": len(after), "active": active, "simulation_assets_added": imported_assets}
    finally:
        if tmp_path:
            try: Path(tmp_path).unlink(missing_ok=True)
            except Exception: pass


def _show_cycle_result(r: dict):
    sim=r.get("simulation",{}) or {}
    health=r.get("health",{}) or {}
    cal=r.get("calibration",{}) or {}
    total=int(health.get("total_pairs",0))
    ready=int(health.get("ready_pairs",0))
    waiting=int(health.get("waiting_history",len(cal.get("waiting_history",[]) or [])))
    errors=int(health.get("true_errors",len(r.get("true_errors",[]) or [])))

    a,b,c,d=st.columns(4)
    a.metric("可運行",f"{ready} / {total}" if total else "0")
    b.metric("等待更多資料",waiting)
    c.metric("真正錯誤",errors)
    d.metric("本次新 K",int(sim.get("bars_processed",0)))

    if errors == 0:
        if waiting:
            st.success(f"系統正常。{ready}/{total} 組已可運行；{waiting} 組只是歷史 K 尚未達門檻，不算錯誤。交易 API 0 次。")
        else:
            st.success(f"系統正常。{ready}/{total} 組可運行；處理 {sim.get('bars_processed',0)} 根新 K；交易 API 0 次。")
    else:
        st.error(f"有 {errors} 個真正錯誤需要處理；其餘等待歷史資料的組合不算故障。")

    waiting_rows=cal.get("waiting_history",[]) or []
    true_errors=r.get("true_errors",[]) or []
    if waiting_rows or true_errors:
        with st.expander("查看未就緒詳細資料"):
            if waiting_rows:
                st.markdown("**等待歷史資料**")
                w=pd.DataFrame(waiting_rows)
                if not w.empty:
                    w=w.rename(columns={"market":"市場","symbol":"標的","horizon":"週期","required_closed_bars":"最低完整 K"})
                    cols=[c for c in ["市場","標的","週期","最低完整 K"] if c in w.columns]
                    st.dataframe(w[cols],use_container_width=True,hide_index=True)
            if true_errors:
                st.markdown("**真正錯誤**")
                st.dataframe(pd.DataFrame(true_errors),use_container_width=True,hide_index=True)


st.title("V6 Live Simulation Dashboard")
st.caption("只看結果與數據｜本地虛擬資金｜交易訂單 API = 0｜行情 SQLite 快取＋節流更新")

model_health=engine.model_health()
c1,c2,c3,c4,c5,c6=st.columns(6)
c1.metric("虛擬帳戶","6")
c2.metric("ACTIVE 標的",len(db.assets()))
c3.metric("可運行組合",f"{model_health['ready_pairs']}/{model_health['total_pairs']}")
c4.metric("尚未就緒",model_health["unready_pairs"])
c5.metric("目前持倉",len(db.positions()))
c6.metric("交易訂單 API","0")

if len(db.assets()) == 0:
    st.warning("雲端目前還沒有研究標的。請在頁面最下方『研究/維護工具』匯入你本機舊 V6 的 forward_validation.sqlite3；匯入後不需要再上傳。")

if st.button("立即完整更新",type="primary",use_container_width=True):
    with st.spinner("自動匯入標的 → 校準到期模型 → 補行情 → 算短中長決策 → 更新本地模擬倉..."):
        r=engine.full_cycle(force_recalibrate=False)
        st.session_state["last_manual_cycle"]=r
r=st.session_state.get("last_manual_cycle")
if r:
    _show_cycle_result(r)

@st.fragment(run_every="30s")
def live_results():
    prices=latest_prices_table(db,engine.cache)
    st.subheader("最新行情（雲端快取）")
    if prices.empty: st.info("尚未建立行情快取。背景 Worker 啟動後會自動補資料。")
    else:
        px=prices.copy(); px["漲跌"]=px.change_pct.map(lambda x:f"{x*100:+.2f}%")
        st.dataframe(px[["bar_time","market","symbol","price","漲跌","volume"]],use_container_width=True,hide_index=True)

    acct=account_performance(db,lab)
    st.subheader("六個等本金帳戶")
    if not acct.empty:
        a=acct.copy(); a["報酬率"]=a.return_pct.map(lambda x:f"{x*100:.2f}%"); a["回撤"]=a.drawdown.map(lambda x:f"{x*100:.2f}%")
        a["勝率"]=a.win_rate.map(lambda x:"—" if pd.isna(x) else f"{x*100:.1f}%"); a["PF"]=a.profit_factor.map(lambda x:"—" if pd.isna(x) else ("∞" if x==float('inf') else f"{x:.2f}"))
        st.dataframe(a[["account_id","initial_equity","equity","報酬率","cash","gross_exposure","leverage","回撤","positions","closed_trades","勝率","PF"]],use_container_width=True,hide_index=True)

    pos=positions_table(db,engine.cache)
    st.subheader("目前持倉")
    if pos.empty: st.info("目前沒有持倉。")
    else:
        p=pos.copy(); p["未實現報酬"]=p.return_pct.map(lambda x:f"{x*100:.2f}%"); p["未實現P/L"]=p.unrealized_pnl.map(lambda x:f"{x:,.2f}")
        st.dataframe(p[["account_id","symbol","週期","strategy","entry_price","mark_price","未實現P/L","未實現報酬","leverage_at_entry","stop_price","target_price","bars_held"]],use_container_width=True,hide_index=True)

    latest=latest_by_asset_horizon(db)
    st.subheader("最新短／中／長決策")
    if latest.empty: st.info("尚未產生決策。")
    else:
        v=latest.sort_values(["action","trade_confidence"],ascending=[True,False]).copy()
        v["Trade信心"]=v.trade_confidence.map(lambda x:f"{x:.1f}"); v["模型信心"]=v.model_confidence.map(lambda x:f"{x:.1f}"); v["訊號強度"]=v.signal_strength.map(lambda x:f"{x:.1f}")
        st.dataframe(v[["bar_time","market","symbol","週期","strategy","action","Trade信心","模型信心","訊號強度","regime","requested_notional","leverage","reason"]],use_container_width=True,hide_index=True)

    problems=problem_ranking(db)
    st.subheader("策略問題排名")
    if problems.empty: st.info("已平倉樣本還太少，等待累積後會自動找出失效組合。")
    else:
        q=problems.copy(); q["勝率"]=q.win_rate.map(lambda x:f"{x*100:.1f}%"); q["平均報酬"]=q.avg_return.map(lambda x:f"{x*100:.2f}%"); q["PF"]=q.profit_factor.map(lambda x:"—" if pd.isna(x) else ("∞" if x==float('inf') else f"{x:.2f}"))
        st.dataframe(q[["symbol","週期","strategy","regime","samples","勝率","PF","平均報酬","realized_pnl","問題"]].head(30),use_container_width=True,hide_index=True)

    trades=pd.DataFrame(db.recent_trades(100))
    st.subheader("最近已平倉")
    if trades.empty: st.info("尚無已平倉交易。")
    else: st.dataframe(trades[[c for c in ["exit_bar","account_id","symbol","strategy","horizon","realized_pnl","return_pct","exit_reason","leverage"] if c in trades.columns]],use_container_width=True,hide_index=True)

    dec=decisions_table(db,500)
    if not dec.empty:
        last=pd.to_datetime(dec.bar_time,utc=True,errors="coerce").max()
        st.caption(f"最新決策資料時間：{last}｜畫面每 30 秒只讀雲端資料庫；背景 Worker 才會依節流規則補行情。")

live_results()

with st.expander("研究/維護工具", expanded=(len(db.assets()) == 0)):
    st.markdown("### 一次性匯入舊 V6 候選")
    st.caption("選你原本本機 V6 資料夾裡的 forward_validation.sqlite3。只會合併候選標的，不會覆蓋目前雲端資料，也不會上傳到 GitHub。")
    uploaded_forward = st.file_uploader("選擇 forward_validation.sqlite3", type=["sqlite3", "db"], key="forward_db_upload")
    if uploaded_forward is not None and st.button("匯入 ACTIVE 候選到雲端", type="primary"):
        try:
            result = _import_forward_candidates(uploaded_forward)
            st.success(f"匯入完成：新增 {result['imported']} 個候選；ACTIVE {result['active']} 個。已同步建立 Simulation 標的。")
            st.session_state["last_import_result"] = result
            st.rerun()
        except Exception as e:
            st.error(f"匯入失敗：{type(e).__name__}: {e}")

    st.divider()
    st.write("完整研究頁仍保留在 app.py。平常只需要這個 Dashboard；要重新大篩選或查 Stage 3/4/5/6 細節時才開研究頁。")
    if st.button("強制重新校準全部模型"):
        with st.spinner("重新校準所有 ACTIVE 標的 × 短中長..."):
            x=engine.calibrate_due(force=True)
        wa=x.get("waiting_history",[]) or []
        er=x.get("errors",[]) or []
        st.write(f"校準完成：成功 {x.get('calibrated',0)}；等待資料 {len(wa)}；真正錯誤 {len(er)}。")
        if wa:
            st.dataframe(pd.DataFrame(wa),use_container_width=True,hide_index=True)
        if er:
            st.error("有真正錯誤需要處理")
            st.dataframe(pd.DataFrame(er),use_container_width=True,hide_index=True)
