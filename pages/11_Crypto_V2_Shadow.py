from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src.crypto_lite_nav import render_crypto_lite_sidebar

from src.crypto_v2.risk import portfolio_status
from src.crypto_v2.shadow_db import CryptoV2ShadowDB
from src.market_cache import MarketCache, TIMEFRAME_MAP
from src.paths import data_dir, db_path

st.set_page_config(page_title="Crypto V2 中文模擬實驗室", layout="wide")
render_crypto_lite_sidebar()
st.title("Crypto V2 中文模擬實驗室")
st.caption("獨立前向模擬：使用同一份市場快取資料，但採用不同策略引擎與獨立帳本，不影響現有 Crypto 原版系統。")

snapshot_path = Path(data_dir()) / "crypto_v2_shadow_snapshot.json"
status_path = Path(data_dir()) / "crypto_v2_shadow_worker_status.json"


市場狀態中文 = {
    "INSUFFICIENT_DATA": "資料不足",
    "PANIC": "恐慌市場",
    "HIGH_VOL_SIDEWAYS": "高波動盤整",
    "TREND_UP": "上升趨勢",
    "TREND_DOWN": "下降趨勢",
    "LOW_VOL_SIDEWAYS": "低波動盤整",
    "SIDEWAYS": "盤整市場",
    "UNKNOWN": "未知",
}

決策中文 = {
    "ENTER": "進場",
    "EXIT": "出場",
    "NO_TRADE": "不交易",
    "BUY": "買進",
    "SELL": "賣出",
    "HOLD": "持有",
}

策略中文 = {
    "V2_MOMENTUM": "V2 動能策略",
    "V2_BREAKOUT": "V2 突破策略",
    "V2_MEAN_REVERSION": "V2 均值回歸策略",
    "NONE": "無",
    "UNKNOWN": "未知",
}

週期中文 = {
    "short": "短線",
    "medium": "中線",
    "long": "長線",
}

執行狀態中文 = {
    "ONLINE": "正常運行",
    "DEGRADED": "部分異常",
    "OFFLINE": "離線",
    "STARTING": "啟動中",
    "ERROR": "錯誤",
}

風控狀態中文 = {
    "NORMAL": "正常",
    "LIMITED": "已限制新部位",
}

風控原因中文 = {
    "MAX_POSITIONS": "同週期持倉／預約槽位已滿",
    "MAX_GROSS_EXPOSURE": "總曝險已達上限",
    "MAX_STRATEGY_EXPOSURE": "同策略曝險已達上限",
    "MAX_REGIME_EXPOSURE": "同市場狀態曝險已達上限",
    "BELOW_MIN_ENTRY": "可用風險額度低於最小有效部位",
    "NO_CAPITAL": "可用資金不足",
    "DOWNSIZED_BY_PORTFOLIO_RISK": "組合風控已縮小部位",
    "APPROVED": "通過",
}

出場原因中文 = {
    "STOP": "停損",
    "TARGET": "停利",
    "TIME": "持有時間到期",
}

市場原因中文 = {
    "Need >=96 BTC 1h bars": "BTC 1 小時 K 線至少需要 96 根資料",
    "BTC drawdown + volatility shock": "BTC 明顯下跌並伴隨波動率衝擊",
    "Volatility elevated without stable trend": "波動率升高，但沒有穩定趨勢",
    "BTC fast EMA above slow EMA with positive 24h return": "BTC 快速 EMA 高於慢速 EMA，且 24 小時報酬為正",
    "BTC fast EMA below slow EMA with negative 24h return": "BTC 快速 EMA 低於慢速 EMA，且 24 小時報酬為負",
    "Volatility compression": "波動率收縮",
    "No dominant directional regime": "目前沒有明顯單一方向趨勢",
}

決策原因中文 = {
    "Insufficient symbol history": "交易對歷史資料不足",
    "Trend-up + positive relative strength": "上升趨勢，且相對強度為正",
    "Volatility compression breakout with volume confirmation": "波動率收縮後突破，且成交量確認",
    "Sideways oversold mean-reversion setup": "盤整市場出現超賣均值回歸條件",
    "Existing V2 shadow position": "目前已有 V2 模擬持倉",
    "Pending V2 shadow entry": "已有等待成交的 V2 模擬進場單",
}


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def zh_market(value) -> str:
    raw = str(value or "UNKNOWN")
    return 市場狀態中文.get(raw, raw)


def zh_action(value) -> str:
    raw = str(value or "")
    return 決策中文.get(raw, raw)


def zh_strategy(value) -> str:
    raw = str(value or "")
    return 策略中文.get(raw, raw)


def zh_horizon(value) -> str:
    raw = str(value or "")
    return 週期中文.get(raw, raw)


def zh_worker(value) -> str:
    raw = str(value or "尚未啟動")
    return 執行狀態中文.get(raw, raw)


def zh_exit_reason(value) -> str:
    raw = str(value or "")
    return 出場原因中文.get(raw, raw)


def zh_market_reason(value) -> str:
    raw = str(value or "")
    return 市場原因中文.get(raw, raw or "等待 V2 第一輪資料")


def zh_risk_reason(value) -> str:
    raw = str(value or "")
    return 風控原因中文.get(raw, raw)


def zh_decision_reason(value) -> str:
    raw = str(value or "")
    if raw in 決策原因中文:
        return 決策原因中文[raw]
    if raw.startswith("Risk-off regime: "):
        state = raw.split(": ", 1)[1]
        return f"風險趨避狀態：{zh_market(state)}，本輪不交易"
    if raw.startswith("No V2 setup in "):
        state = raw.removeprefix("No V2 setup in ")
        return f"{zh_market(state)}目前沒有符合 V2 條件的交易機會"
    if raw.startswith("Portfolio risk governor blocked entry: "):
        reason = raw.removeprefix("Portfolio risk governor blocked entry: ")
        return f"組合風控阻擋進場：{zh_risk_reason(reason)}"
    suffix = "; portfolio risk governor downsized entry"
    if raw.endswith(suffix):
        base = raw[: -len(suffix)]
        base_zh = 決策原因中文.get(base, base)
        return f"{base_zh}；組合風控已縮小部位"
    return raw


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
c1.metric("V2 模擬報酬", "—" if v2_return is None else f"{v2_return*100:.2f}%")
c2.metric("原版 Crypto 報酬", "—" if baseline.get("return_pct") is None else f"{float(baseline['return_pct'])*100:.2f}%")
c3.metric("V2 已平倉筆數", f"{v2_closed}")
c4.metric("V2 執行狀態", zh_worker(status.get("status") or snapshot.get("status") or "尚未啟動"))

regime = snapshot.get("latest_market_regime") or {}
st.subheader("目前 Crypto 市場環境")
r1, r2, r3, r4 = st.columns(4)
r1.metric("市場狀態", zh_market(regime.get("state")))
r2.metric("BTC 趨勢強度", f"{float(regime.get('trend') or 0)*100:.2f}%")
r3.metric("波動率倍數", f"{float(regime.get('vol_ratio') or 0):.2f} 倍")
r4.metric("BTC 24 小時報酬", f"{float(regime.get('ret_slow') or 0)*100:.2f}%")
st.caption(zh_market_reason(regime.get("reason")))

st.subheader("V2 組合風控")
risk_rows = []
for h in ("short", "medium", "long"):
    risk = portfolio_status(shadow.initial_equity, shadow.portfolio_state(h), h)
    reasons = "、".join(zh_risk_reason(x) for x in risk.get("breaches") or []) or "無"
    limits = risk.get("limits") or {}
    risk_rows.append({
        "交易週期": zh_horizon(h),
        "風控狀態": 風控狀態中文.get(str(risk.get("status") or ""), str(risk.get("status") or "")),
        "目前持倉": int(risk.get("open_positions") or 0),
        "等待成交": int(risk.get("pending_orders") or 0),
        "總曝險%": float(risk.get("gross_pct") or 0.0) * 100,
        "總曝險上限%": float(limits.get("max_gross_pct") or 0.0) * 100,
        "同策略上限%": float(limits.get("max_strategy_pct") or 0.0) * 100,
        "同市場狀態上限%": float(limits.get("max_regime_pct") or 0.0) * 100,
        "最多部位／預約": int(limits.get("max_positions") or 0),
        "目前限制原因": reasons,
    })
st.dataframe(pd.DataFrame(risk_rows), width="stretch", hide_index=True)
st.caption("組合風控會同時計入已持倉與尚未成交的進場單；達到上限時只會阻擋或縮小新部位，不會強制平掉既有 Shadow 持倉。")

st.subheader("原版 vs V2 — 各交易週期")
base_map = {x.get("horizon"): x for x in (baseline.get("accounts") or [])}
rows = []
for v in v2_accounts:
    b = base_map.get(v["horizon"], {})
    rows.append({
        "交易週期": zh_horizon(v["horizon"]),
        "原版報酬%": None if b.get("return_pct") is None else float(b["return_pct"]) * 100,
        "原版已平倉": int(b.get("closed_trades") or 0),
        "V2 報酬%": None if v.get("return_pct") is None else float(v["return_pct"]) * 100,
        "V2 已平倉": int(v.get("closed_trades") or 0),
        "V2 目前持倉": int(v.get("open_positions") or 0),
        "V2 已實現損益": float(v.get("realized_pnl") or 0.0),
    })
st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

st.subheader("V2 目前持倉")
pos = pd.DataFrame(shadow.positions())
if pos.empty:
    st.info("目前沒有 V2 模擬持倉。『不交易』本身也是 V2 的有效決策，不代表系統沒有運行。")
else:
    if "horizon" in pos.columns:
        pos["horizon"] = pos["horizon"].map(zh_horizon)
    if "strategy" in pos.columns:
        pos["strategy"] = pos["strategy"].map(zh_strategy)
    if "regime_entry" in pos.columns:
        pos["regime_entry"] = pos["regime_entry"].map(zh_market)
    pos = pos.rename(columns={
        "symbol": "交易對",
        "horizon": "交易週期",
        "qty": "持倉數量",
        "avg_entry": "平均進場價",
        "entry_bar": "進場時間",
        "strategy": "使用策略",
        "regime_entry": "進場市場狀態",
        "stop_price": "停損價",
        "target_price": "停利價",
        "max_holding_bars": "最長持有 K 線數",
        "bars_held": "目前已持有 K 線數",
    })
    st.dataframe(pos, width="stretch", hide_index=True)

st.subheader("最近 V2 決策")
decisions = pd.DataFrame(shadow.recent_decisions(150))
if decisions.empty:
    st.info("尚未產生 V2 決策。")
else:
    if "horizon" in decisions.columns:
        decisions["horizon"] = decisions["horizon"].map(zh_horizon)
    if "regime" in decisions.columns:
        decisions["regime"] = decisions["regime"].map(zh_market)
    if "action" in decisions.columns:
        decisions["action"] = decisions["action"].map(zh_action)
    if "strategy" in decisions.columns:
        decisions["strategy"] = decisions["strategy"].map(zh_strategy)
    if "reason" in decisions.columns:
        decisions["reason"] = decisions["reason"].map(zh_decision_reason)
    if "confidence" in decisions.columns:
        decisions["confidence"] = pd.to_numeric(decisions["confidence"], errors="coerce") * 100.0

    cols = [x for x in ["bar_time", "symbol", "horizon", "regime", "action", "strategy", "confidence", "reason"] if x in decisions.columns]
    decisions = decisions[cols].rename(columns={
        "bar_time": "決策時間",
        "symbol": "交易對",
        "horizon": "交易週期",
        "regime": "市場狀態",
        "action": "決策",
        "strategy": "策略",
        "confidence": "信心度%",
        "reason": "決策原因",
    })
    st.dataframe(decisions, width="stretch", hide_index=True)

st.subheader("最近 V2 已平倉交易")
trades = pd.DataFrame(shadow.recent_trades(100))
if trades.empty:
    st.info("V2 是從啟用後才開始累積真實前向模擬資料，目前尚無平倉交易屬正常。")
else:
    if "horizon" in trades.columns:
        trades["horizon"] = trades["horizon"].map(zh_horizon)
    if "strategy" in trades.columns:
        trades["strategy"] = trades["strategy"].map(zh_strategy)
    if "regime_entry" in trades.columns:
        trades["regime_entry"] = trades["regime_entry"].map(zh_market)
    if "exit_reason" in trades.columns:
        trades["exit_reason"] = trades["exit_reason"].map(zh_exit_reason)
    if "return_pct" in trades.columns:
        trades["return_pct"] = pd.to_numeric(trades["return_pct"], errors="coerce") * 100.0

    cols = [x for x in [
        "symbol", "horizon", "entry_bar", "exit_bar", "qty", "entry_price", "exit_price",
        "realized_pnl", "return_pct", "strategy", "regime_entry", "exit_reason"
    ] if x in trades.columns]
    trades = trades[cols].rename(columns={
        "symbol": "交易對",
        "horizon": "交易週期",
        "entry_bar": "進場時間",
        "exit_bar": "出場時間",
        "qty": "交易數量",
        "entry_price": "進場價",
        "exit_price": "出場價",
        "realized_pnl": "已實現損益",
        "return_pct": "單筆報酬%",
        "strategy": "策略",
        "regime_entry": "進場市場狀態",
        "exit_reason": "出場原因",
    })
    st.dataframe(trades, width="stretch", hide_index=True)

st.caption(
    "Crypto V2 目前僅進行模擬驗證：不呼叫券商或交易所下單 API、不額外抓取行情，也不回填啟用前的交易成果。"
)
