from __future__ import annotations

import re

MARKET = {
    "crypto": "加密貨幣",
    "stock": "美股",
    "twstock": "台股",
    "GLOBAL": "全系統",
}

HORIZON = {"short": "短線", "medium": "中線", "long": "長線"}

STRATEGY = {
    "Momentum": "動能策略",
    "Trend MA": "均線趨勢",
    "Mean Reversion RSI": "RSI 均值回歸",
    "RSI Mean Reversion": "RSI 均值回歸",
    "Breakout": "突破策略",
}

ACTION = {
    "ENTER": "進場",
    "EXIT": "出場",
    "NO_TRADE": "不交易",
    "HOLD": "持有",
    "BUY": "買進",
    "SELL": "賣出",
    "WATCH": "觀察",
}

STATUS = {
    "ONLINE": "在線",
    "OFFLINE": "離線",
    "RUNNING": "執行中",
    "STARTING": "啟動中",
    "WAITING": "等待中",
    "DEGRADED": "部分異常",
    "ERROR": "錯誤",
    "NO_KEYS": "缺少金鑰",
    "BAR_ONLY": "僅K線",
    "ACTIVE": "啟用",
}

RISK = {"LOW": "低", "MEDIUM": "中", "HIGH": "高", "CRITICAL": "極高"}

VERDICT = {"ALLOW": "允許", "CAUTION": "注意", "BLOCK_CANDIDATE": "建議阻擋"}

HEALTH_STATE = {
    "LEARNING": "樣本累積中",
    "NORMAL": "正常",
    "WATCH": "觀察",
    "SHADOW_ONLY_CANDIDATE": "建議僅影子觀察",
    "PAUSE_CANDIDATE": "建議暫停",
}

EXIT_REASON = {
    "MODEL_EXIT": "模型出場",
    "ATR_STOP": "ATR 停損",
    "ATR_TARGET": "ATR 停利目標",
    "TIME_EXIT": "時間到期出場",
    "MARGIN_LIQUIDATION": "保證金強制平倉",
}

REALTIME_SIGNAL = {
    "HOLD": "持有監控",
    "NEAR_STOP": "接近停損",
    "STOP_TOUCH": "觸及停損",
    "NEAR_TARGET": "接近目標",
    "TARGET_TOUCH": "觸及目標",
    "ENTRY_CONFIRM": "確認進場",
    "WATCH": "觀察",
}

REGIME_EXACT = {
    "HIGH_VOL_UP_TREND": "高波動上升趨勢",
    "HIGH_VOL_DOWN_TREND": "高波動下降趨勢",
    "LOW_VOL_UP_TREND": "低波動上升趨勢",
    "LOW_VOL_DOWN_TREND": "低波動下降趨勢",
    "HIGH_VOL_RANGE": "高波動盤整",
    "LOW_VOL_RANGE": "低波動盤整",
    "UP_TREND": "上升趨勢",
    "DOWN_TREND": "下降趨勢",
    "RANGE": "盤整",
    "UNKNOWN": "未知",
}

TOKEN_REPLACEMENTS = {
    "HIGH_VOL": "高波動",
    "LOW_VOL": "低波動",
    "UP_TREND": "上升趨勢",
    "DOWN_TREND": "下降趨勢",
    "TREND": "趨勢",
    "RANGE": "盤整",
    "VOL": "波動",
    "MODEL_EXIT": "模型出場",
    "ATR_STOP": "ATR停損",
    "ATR_TARGET": "ATR停利目標",
    "TIME_EXIT": "時間出場",
    "MARGIN_LIQUIDATION": "強制平倉",
    "TOP_CONFIDENCE": "高信心候選",
    "ACTIVE_FALLBACK": "啟用標的補位",
    "POSITION_MULTI_HORIZON": "多週期持倉",
    "POSITION": "持倉",
    "ENTER": "進場",
    "EXIT": "出場",
    "WATCH": "觀察",
}


def market_label(value):
    return MARKET.get(str(value), str(value))


def horizon_label(value):
    return HORIZON.get(str(value), str(value))


def strategy_label(value):
    return STRATEGY.get(str(value), str(value))


def action_label(value):
    return ACTION.get(str(value), str(value))


def status_label(value):
    return STATUS.get(str(value).upper(), str(value))


def risk_label(value):
    return RISK.get(str(value).upper(), str(value))


def verdict_label(value):
    return VERDICT.get(str(value).upper(), str(value))


def health_label(value):
    return HEALTH_STATE.get(str(value).upper(), str(value))


def exit_reason_label(value):
    return EXIT_REASON.get(str(value).upper(), translate_code(value))


def realtime_signal_label(value):
    return REALTIME_SIGNAL.get(str(value).upper(), translate_code(value))


def regime_label(value):
    s = str(value or "")
    if s in REGIME_EXACT:
        return REGIME_EXACT[s]
    return translate_code(s)


def account_label(value):
    s = str(value or "")
    for market in ("crypto", "stock", "twstock"):
        prefix = market + "_"
        if s.startswith(prefix):
            h = s[len(prefix):]
            return f"{market_label(market)}－{horizon_label(h)}"
    return s


def bool_label(value):
    return "是" if bool(value) else "否"


def translate_code(value):
    s = str(value or "")
    if not s:
        return "—"
    if s in REGIME_EXACT:
        return REGIME_EXACT[s]
    if s in ACTION:
        return ACTION[s]
    if s in STATUS:
        return STATUS[s]
    if s in EXIT_REASON:
        return EXIT_REASON[s]
    if s in REALTIME_SIGNAL:
        return REALTIME_SIGNAL[s]
    out = s
    for old, new in sorted(TOKEN_REPLACEMENTS.items(), key=lambda kv: len(kv[0]), reverse=True):
        out = out.replace(old, new)
    out = out.replace("_", " ")
    return re.sub(r"\s+", " ", out).strip()


def translate_reason(value):
    s = str(value or "")
    if not s:
        return "—"
    replacements = {
        "qualified_signal": "訊號符合進場條件",
        "no_signal": "目前沒有交易訊號",
        "model_exit": "模型判定出場",
        "risk": "風險",
        "confidence": "信心",
        "signal": "訊號",
        "regime": "市場狀態",
    }
    out = s
    for old, new in replacements.items():
        out = out.replace(old, new).replace(old.upper(), new)
    return translate_code(out)
