from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src.crypto_v2.shadow_db import CryptoV2ShadowDB
from src.market_cache import MarketCache, TIMEFRAME_MAP
from src.paths import data_dir, db_path

st.set_page_config(page_title="Crypto V2 Shadow", layout="wide")
st.title("Crypto V2 Shadow Lab")
st.caption("獨立 Forward Shadow：同一份市場 cache、不同策略引擎與帳本，不影響現有 Crypto baseline。")

snapshot_path = Path(data_dir()) / "crypto_v2_shadow_snapshot.json"
status_path = Path(data_dir()) / "crypto_v2_shadow_worker_status.json"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


snapshot = load_json(snapshot_path)
status = load_json(status_path)
shadow = CryptoV2ShadowDB(db_path("crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
cache = MarketCache(db_path("market_cache.sqlite3"))

v2_accounts = []
for row in shadow.summary().get("accounts", []):
    h = row["horizon"]
    marks = {}
    _, tf = TIMEFRAME_MAP[("crypto", h)]
    for p in [x for x in shadow.positions() if x.get("horizon") == h]:
        df = cache.get("crypto", str(p.get("symbol") or ""), tf)
        if df is not None and not df.empty:
            marks[str(p.get("symbol"))] = float(df.close.iloc[-1])
    equity = shadow.equity(h, marks)
    initial = float(row.get("initial_equity") or 100000.0)
    v2_accounts.append({
        **row,
        "equity": equity,
        "return_pct": equity / initial - 1.0 if initial else None,
    })

baseline = snapshot.get("baseline") or {}
v2_initial = sum(float(x.get("initial_equity") or 0.0) for x in v2_accounts)
v2_equity = sum(float(x.get("equity") or 0.0) for x in v2_accounts)
v2_return = v2_equity / v2_initial - 1.0 if v2_initial else None
v2_closed = sum(int(x.get("closed_trades") or 0) for x in v2_accounts)

c1, c2, c3, c4 = st.columns(4)
c1.metric("V2 Shadow 報酬", "—" if v2_return is None else f"{v2_return*100:.2f}%")
c2.metric("Baseline Crypto 報酬", "—" if baseline.get("return_pct") is None else f"{float(baseline['return_pct'])*100:.2f}%")
c3.metric("V2 已平倉", f"{v2_closed}")
c4.metric("V2 Worker", str(status.get("status") or snapshot.get("status") or "尚未啟動"))

regime = snapshot.get("latest_market_regime") or {}
st.subheader("目前 Crypto 市場環境")
r1, r2, r3, r4 = st.columns(4)
r1.metric("Regime", str(regime.get("state") or "—"))
r2.metric("BTC trend", f"{float(regime.get('trend') or 0)*100:.2f}%")
r3.metric("Vol ratio", f"{float(regime.get('vol_ratio') or 0):.2f}x")
r4.metric("BTC 24h", f"{float(regime.get('ret_slow') or 0)*100:.2f}%")
st.caption(str(regime.get("reason") or "等待 V2 第一輪資料"))

st.subheader("Baseline vs V2 — 各週期")
base_map = {x.get("horizon"): x for x in (baseline.get("accounts") or [])}
rows = []
for v in v2_accounts:
    b = base_map.get(v["horizon"], {})
    rows.append({
        "週期": v["horizon"],
        "Baseline 報酬%": None if b.get("return_pct") is None else float(b["return_pct"]) * 100,
        "Baseline 平倉": int(b.get("closed_trades") or 0),
        "V2 報酬%": None if v.get("return_pct") is None else float(v["return_pct"]) * 100,
        "V2 平倉": int(v.get("closed_trades") or 0),
        "V2 開倉": int(v.get("open_positions") or 0),
        "V2 已實現損益": float(v.get("realized_pnl") or 0.0),
    })
st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

st.subheader("V2 目前持倉")
pos = pd.DataFrame(shadow.positions())
if pos.empty:
    st.info("目前沒有 V2 Shadow 持倉。NO_TRADE 本身也是 V2 的有效決策。")
else:
    st.dataframe(pos, width="stretch", hide_index=True)

st.subheader("最近 V2 決策")
decisions = pd.DataFrame(shadow.recent_decisions(150))
if decisions.empty:
    st.info("尚未產生 V2 決策。")
else:
    cols = [x for x in ["bar_time", "symbol", "horizon", "regime", "action", "strategy", "confidence", "reason"] if x in decisions.columns]
    st.dataframe(decisions[cols], width="stretch", hide_index=True)

st.subheader("最近 V2 平倉")
trades = pd.DataFrame(shadow.recent_trades(100))
if trades.empty:
    st.info("V2 是從啟用後才開始累積 forward evidence，目前尚無平倉屬正常。")
else:
    st.dataframe(trades, width="stretch", hide_index=True)

st.caption(
    "Crypto V2 目前只做 Shadow：不呼叫券商/交易所下單 API；不額外抓行情；不回填啟用前的交易成果。"
)
