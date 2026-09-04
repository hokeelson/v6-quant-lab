from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from src.crypto_lite_nav import render_crypto_lite_sidebar
import yaml
from dotenv import load_dotenv

from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.dashboard_direction_fallback import build_cached_direction_fallback
from src.ui_zh import horizon_label
from src.worker_progress_ui import render_worker_progress
from src.execution_audit_ui import render_execution_audit

load_dotenv()
st.set_page_config(page_title="V6 Crypto Lite", layout="wide", page_icon="₿")
render_crypto_lite_sidebar()

_required_password = os.getenv("V6_DASHBOARD_PASSWORD", "") or os.getenv("V6_PASSWORD", "")
if _required_password and not st.session_state.get("v6_authenticated", False):
    st.title("V6 Crypto Lite")
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

INITIAL_CAPITAL = float((cfg.get("research") or {}).get("initial_capital", 100000))
MAX_POSITION_PCT = float((cfg.get("risk") or {}).get("max_position_pct", 0.20))

engine = AutoOrchestratorV8(INITIAL_CAPITAL)
db = engine.db


def _load_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _snapshot_stale(snapshot: dict, max_age_seconds: int = 1800) -> bool:
    if not snapshot or not snapshot.get("rows"):
        return True
    try:
        dt = datetime.fromisoformat(str(snapshot.get("generated_at") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > max_age_seconds
    except Exception:
        return True


@st.cache_data(ttl=300, show_spinner=False)
def _cached_direction_fallback() -> dict:
    return build_cached_direction_fallback(db, engine.cache)


def _direction_zh(v: str) -> str:
    return {"LONG": "做多", "SHORT": "做空", "NO_TRADE": "不交易"}.get(str(v), "資料不足")


def _direction_summary(rows: list[dict]) -> tuple[str, int, int, int]:
    long_n = sum(1 for r in rows if r.get("direction") == "LONG")
    short_n = sum(1 for r in rows if r.get("direction") == "SHORT")
    no_n = sum(1 for r in rows if r.get("direction") == "NO_TRADE")
    if not rows:
        return "NO_DATA", 0, 0, 0
    if no_n >= max(long_n, short_n) and no_n >= len(rows) * 0.45:
        return "NO_TRADE", long_n, short_n, no_n
    if long_n > short_n * 1.15:
        return "LONG", long_n, short_n, no_n
    if short_n > long_n * 1.15:
        return "SHORT", long_n, short_n, no_n
    return "NO_TRADE", long_n, short_n, no_n


def _select_symbol_winners(rows: list[dict]) -> list[dict]:
    """Keep one best short/medium/long direction candidate per symbol."""
    winners = {}
    for r in rows:
        if r.get("direction") not in ("LONG", "SHORT"):
            continue
        confidence = float(r.get("direction_confidence") or 0.0)
        ev_gap = max(0.0, float(r.get("ev_gap_r") or 0.0))
        stability = float(r.get("stability_score") or 0.0)
        score = 0.50 * confidence + 0.30 * min(ev_gap / 0.50, 1.0) + 0.20 * stability
        row = dict(r)
        row["_adaptive_score"] = score
        symbol = str(row.get("symbol") or "")
        current = winners.get(symbol)
        if current is None or score > float(current.get("_adaptive_score") or 0.0):
            winners[symbol] = row
    return sorted(
        winners.values(),
        key=lambda r: (
            float(r.get("_adaptive_score") or 0.0),
            float(r.get("direction_confidence") or 0.0),
        ),
        reverse=True,
    )


def _stats(research: dict) -> dict:
    shadow = research.get("direction_shadow") or {}
    stats = shadow.get("decision_stats") or shadow.get("by_decision") or {}
    return stats if isinstance(stats, dict) else {}


@st.fragment(run_every="30s")
def crypto_lite():
    direction = _load_json(Path("static") / "direction_shadow_snapshot.json")
    research = _load_json(Path("static") / "research_snapshot.json")
    health = _load_json(Path("static") / "runtime_health.json")

    if _snapshot_stale(direction):
        fallback = _cached_direction_fallback()
        if fallback.get("rows"):
            direction = fallback

    rows = [r for r in (direction.get("rows") or []) if str(r.get("market")) == "crypto"]
    market_dir, long_n, short_n, no_n = _direction_summary(rows)

    st.title("V6 Crypto Lite")
    st.caption("單一 NTD 100,000 模擬資金視角｜只分析加密貨幣｜短 / 中 / 長線共用同一套資金與風控概念｜Paper / Shadow only")

    overall = str(health.get("overall_status") or "UNKNOWN")
    broker_calls = ((health.get("safety") or {}).get("broker_order_api_calls"))
    if overall == "HEALTHY" and broker_calls == 0:
        st.success("系統正常｜不送真實訂單｜Broker order API calls = 0")
    elif health:
        st.warning(f"系統狀態：{overall}｜Broker order API calls：{broker_calls if broker_calls is not None else '—'}")
    else:
        st.info("正在等待最新系統狀態。")

    render_execution_audit(st, research)

    main_worker = ((health.get("components") or {}).get("main_v8") or {})
    if main_worker and str(main_worker.get("status")) not in ("ONLINE", "HEALTHY", "OK"):
        render_worker_progress(st, main_worker)

    account = db.account("crypto") or {}
    positions = db.positions("crypto")
    marks = db.marks("crypto")
    cash = float(account.get("cash") or 0.0)
    position_rows = []
    gross_exposure = 0.0
    unrealized_total = 0.0
    for p in positions:
        qty = float(p.get("qty") or 0.0)
        entry = float(p.get("avg_entry") or 0.0)
        mark = float(marks.get(str(p.get("symbol") or "").upper(), entry) or entry or 0.0)
        market_value = qty * mark
        exposure = abs(market_value)
        unrealized = qty * (mark - entry)
        gross_exposure += exposure
        unrealized_total += unrealized
        position_rows.append({
            "標的": p.get("symbol"),
            "方向": "做多" if qty > 0 else ("做空" if qty < 0 else "—"),
            "週期": horizon_label(p.get("horizon")),
            "數量": qty,
            "進場價": entry,
            "現價": mark,
            "持倉金額": exposure,
            "未實現損益": unrealized,
            "持倉比例": 0.0,
            "策略": p.get("strategy") or "—",
        })
    total_equity = cash + sum(float(p.get("qty") or 0.0) * float(marks.get(str(p.get("symbol") or "").upper(), p.get("avg_entry") or 0.0) or 0.0) for p in positions)
    if total_equity:
        for row in position_rows:
            row["持倉比例"] = float(row["持倉金額"]) / abs(total_equity)

    st.subheader("目前持倉")
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("帳戶總資產", f"NTD {total_equity:,.0f}")
    pc2.metric("可用現金", f"NTD {cash:,.0f}")
    pc3.metric("持倉曝險", f"NTD {gross_exposure:,.0f}")
    pc4.metric("未實現損益", f"NTD {unrealized_total:+,.0f}")
    if not position_rows:
        st.info("目前沒有模擬持倉。")
    else:
        pos_df = pd.DataFrame(position_rows)
        pos_df["數量"] = pos_df["數量"].map(lambda x: f"{x:,.6f}")
        pos_df["進場價"] = pos_df["進場價"].map(lambda x: f"{x:,.6f}")
        pos_df["現價"] = pos_df["現價"].map(lambda x: f"{x:,.6f}")
        pos_df["持倉金額"] = pos_df["持倉金額"].map(lambda x: f"NTD {x:,.0f}")
        pos_df["未實現損益"] = pos_df["未實現損益"].map(lambda x: f"NTD {x:+,.0f}")
        pos_df["持倉比例"] = pos_df["持倉比例"].map(lambda x: f"{x * 100:.1f}%")
        st.dataframe(pos_df, width="stretch", hide_index=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("模擬本金", f"NTD {INITIAL_CAPITAL:,.0f}")
    c2.metric("目前方向", _direction_zh(market_dir))
    c3.metric("可做多訊號", long_n)
    c4.metric("可做空訊號", short_n)
    st.caption(f"目前不交易訊號：{no_n}｜單一標的參考部位上限：NTD {INITIAL_CAPITAL * MAX_POSITION_PCT:,.0f}")

    qualified = _select_symbol_winners([
        r for r in rows
        if r.get("direction") in ("LONG", "SHORT")
        and float(r.get("direction_confidence") or 0.0) >= 0.55
        and float(r.get("ev_gap_r") or 0.0) >= 0.08
    ])

    st.subheader("現在最值得看的機會")
    st.caption("同一個幣只保留短 / 中 / 長線中分數最高的一個，避免三個週期同時搶同一筆資金。")
    if not qualified:
        st.info("目前沒有同時通過方向信心與 EV 差距門檻的標的，維持不交易。")
    else:
        table = []
        for r in qualified[:12]:
            confidence = float(r.get("direction_confidence") or 0.0)
            ref_notional = min(
                INITIAL_CAPITAL * MAX_POSITION_PCT,
                INITIAL_CAPITAL * MAX_POSITION_PCT * max(0.5, confidence),
            )
            table.append({
                "標的": r.get("symbol"),
                "週期": horizon_label(r.get("horizon")),
                "方向": _direction_zh(r.get("direction")),
                "信心": f"{confidence * 100:.1f}%",
                "EV差距": f"{float(r.get('ev_gap_r') or 0.0):.2f}R",
                "Long EV": f"{float(r.get('long_ev_proxy_r') or 0.0):+.2f}R",
                "Short EV": f"{float(r.get('short_ev_proxy_r') or 0.0):+.2f}R",
                "穩定度": f"{float(r.get('stability_score') or 0.0) * 100:.1f}%",
                "參考部位上限": f"NTD {ref_notional:,.0f}",
                "策略": r.get("preferred_playbook") or r.get("strategy"),
            })
        st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True)

    st.subheader("短 / 中 / 長線怎麼分")
    horizon_rows = []
    for hz in ("short", "medium", "long"):
        sub = [r for r in rows if str(r.get("horizon")) == hz]
        d, ln, sn, nn = _direction_summary(sub)
        best = sorted(
            [r for r in sub if r.get("direction") in ("LONG", "SHORT")],
            key=lambda r: float(r.get("direction_confidence") or 0.0),
            reverse=True,
        )
        horizon_rows.append({
            "週期": horizon_label(hz),
            "整體方向": _direction_zh(d),
            "做多": ln,
            "做空": sn,
            "不做": nn,
            "最強標的": best[0].get("symbol") if best else "—",
            "最高信心": f"{float(best[0].get('direction_confidence') or 0.0) * 100:.1f}%" if best else "—",
        })
    st.dataframe(pd.DataFrame(horizon_rows), width="stretch", hide_index=True)

    stats = _stats(research)
    if stats:
        st.subheader("最近 Forward 證據")
        cols = st.columns(3)
        for col, key, label in zip(cols, ("LONG", "SHORT", "NO_TRADE"), ("做多", "做空", "不交易")):
            row = stats.get(key) or {}
            avg = row.get("avg_forward_return_pct")
            if avg is None:
                avg = row.get("avg_forward_reward_pct")
            hit = row.get("hit_rate")
            if hit is None:
                hit = row.get("hit_rate_pct")
                if hit is not None and float(hit) > 1:
                    hit = float(hit) / 100.0
            completed = row.get("evaluated")
            if completed is None:
                completed = row.get("completed")
            col.metric(label, f"{float(avg or 0.0) * 100:+.2f}%")
            col.caption(f"完成 {int(completed or 0)} 筆｜命中率 {float(hit or 0.0) * 100:.1f}%")

    with st.expander("查看全部加密貨幣訊號"):
        if not rows:
            st.info("目前尚無加密貨幣方向資料。")
        else:
            all_df = pd.DataFrame([{
                "標的": r.get("symbol"),
                "週期": horizon_label(r.get("horizon")),
                "方向": _direction_zh(r.get("direction")),
                "信心": f"{float(r.get('direction_confidence') or 0.0) * 100:.1f}%",
                "EV差距": f"{float(r.get('ev_gap_r') or 0.0):.2f}R",
                "原因": "、".join(r.get("decision_reasons") or []),
            } for r in rows])
            st.dataframe(all_df, width="stretch", hide_index=True)

    st.caption("舊版多市場 / 六帳戶資料仍保留在研究資料庫作歷史證據，不再出現在日常操作畫面，也不會因此送出任何真實交易。")


crypto_lite()
