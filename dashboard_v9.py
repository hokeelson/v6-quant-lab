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
from src.realtime_layer import RealtimeDB
from src.paths import db_path

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


def _fresh_quote_map(max_age_seconds: int = 60) -> dict[str, dict]:
    """Return fresh crypto realtime quotes keyed by symbol; stale rows are ignored."""
    try:
        rt = RealtimeDB(db_path("realtime_execution.sqlite3"))
        now = datetime.now(timezone.utc)
        out = {}
        for q in rt.quotes():
            if str(q.get("market") or "") != "crypto":
                continue
            symbol = str(q.get("symbol") or "").upper()
            price = q.get("price")
            ts_raw = str(q.get("ts") or "")
            if not symbol or price is None:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())
            except Exception:
                continue
            if age > max_age_seconds:
                continue
            out[symbol] = {
                "price": float(price),
                "ts": ts_raw,
                "age_seconds": age,
                "source": str(q.get("source") or "REALTIME"),
            }
        return out
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


@st.fragment(run_every="10s")
def crypto_lite():
    direction = _load_json(Path("static") / "direction_shadow_snapshot.json")
    research = _load_json(Path("static") / "research_snapshot.json")
    health = _load_json(Path("static") / "runtime_health.json")
    binance_context = _load_json(Path("static") / "binance_market_context.json")

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

    main_worker = ((health.get("components") or {}).get("main_v8") or {})
    if overall != "HEALTHY":
        render_execution_audit(st, research)
        if main_worker:
            render_worker_progress(st, main_worker)
    else:
        with st.expander("系統診斷（正常時可忽略）", expanded=False):
            render_execution_audit(st, research)

    st.caption("畫面每 10 秒自動更新｜持倉現價優先使用秒級 Realtime quote｜底層健康狀態約每 5 秒更新")

    with st.expander("Binance 市場資料", expanded=False):
        if not binance_context:
            st.info("正在等待 Binance 市場資料。")
        else:
            bc1, bc2, bc3 = st.columns(3)
            bc1.metric("資料狀態", str(binance_context.get("status") or "UNKNOWN"))
            bc2.metric("Spot Depth 覆蓋", f"{float(binance_context.get('spot_depth_coverage') or 0.0) * 100:.0f}%")
            bc3.metric("Futures 覆蓋", f"{float(binance_context.get('futures_coverage') or 0.0) * 100:.0f}%")
            risky = sorted(
                list(binance_context.get("rows") or []),
                key=lambda r: float(r.get("risk_score") or 0.0),
                reverse=True,
            )[:10]
            if risky:
                st.dataframe(pd.DataFrame([{
                    "標的": r.get("symbol"),
                    "風險狀態": r.get("risk_state"),
                    "風險分數": f"{float(r.get('risk_score') or 0.0) * 100:.0f}%",
                    "Funding": "—" if r.get("last_funding_rate") is None else f"{float(r.get('last_funding_rate')) * 100:.4f}%",
                    "OI": "—" if r.get("open_interest") is None else f"{float(r.get('open_interest')):,.0f}",
                    "多空比": "—" if r.get("long_short_ratio") is None else f"{float(r.get('long_short_ratio')):.2f}",
                    "買盤占比": "—" if r.get("bid_share_top20") is None else f"{float(r.get('bid_share_top20')) * 100:.1f}%",
                    "Spread": "—" if r.get("spread_bps") is None else f"{float(r.get('spread_bps')):.2f} bps",
                    "部位倍率": f"{float(r.get('size_multiplier') or 1.0):.2f}x",
                } for r in risky]), width="stretch", hide_index=True)

    account = db.account("crypto") or {}
    positions = db.positions("crypto")
    marks = db.marks("crypto")
    realtime_quotes = _fresh_quote_map(max_age_seconds=60)
    cash = float(account.get("cash") or 0.0)
    position_rows = []
    gross_exposure = 0.0
    unrealized_total = 0.0
    signed_market_value = 0.0
    realtime_used = 0
    for p in positions:
        qty = float(p.get("qty") or 0.0)
        entry = float(p.get("avg_entry") or 0.0)
        symbol = str(p.get("symbol") or "").upper()
        fallback_mark = float(marks.get(symbol, entry) or entry or 0.0)
        quote = realtime_quotes.get(symbol)
        if quote is not None:
            mark = float(quote["price"])
            price_source = "即時"
            realtime_used += 1
        else:
            mark = fallback_mark
            price_source = "Bar"
        market_value = qty * mark
        exposure = abs(market_value)
        unrealized = qty * (mark - entry)
        gross_exposure += exposure
        unrealized_total += unrealized
        signed_market_value += market_value
        position_rows.append({
            "標的": p.get("symbol"),
            "方向": "做多" if qty > 0 else ("做空" if qty < 0 else "—"),
            "週期": horizon_label(p.get("horizon")),
            "數量": qty,
            "進場價": entry,
            "現價": mark,
            "價格來源": price_source,
            "持倉金額": exposure,
            "未實現損益": unrealized,
            "持倉比例": 0.0,
            "策略": p.get("strategy") or "—",
        })
    total_equity = cash + signed_market_value
    if total_equity:
        for row in position_rows:
            row["持倉比例"] = float(row["持倉金額"]) / abs(total_equity)

    st.subheader("目前持倉")
    if positions:
        st.caption(f"即時估值：{realtime_used}/{len(positions)} 筆持倉使用 60 秒內 Realtime quote；其餘自動退回最近 Bar 價。")
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

    st.caption("目前為清理後的 Crypto Lite 單一主線基準；只保留現在主帳戶與必要 Crypto 模型/風控資料，不送出任何真實交易。")


crypto_lite()
