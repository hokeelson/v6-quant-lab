"""Fail-closed admission for new paper positions; never applies to exits."""
from __future__ import annotations

import math

ENTRY_POLICY_VERSION = "ENTRY_GATE_V1"
BLOCKING_STATES = {"BLOCK", "BLOCK_CANDIDATE", "SHADOW_ONLY", "SHADOW_ONLY_CANDIDATE",
                   "PAUSE", "PAUSE_CANDIDATE", "QUARANTINED"}


def multiplier(value, default=1.0):
    value = default if value is None else value
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("invalid risk multiplier")
    return number


def finalize_entry(result):
    reasons = []
    for key in ("pretrade_verdict", "strategy_state", "regime_state",
                "symbol_strategy_state", "expected_live_state", "meta_verdict"):
        if str(result.get(key) or "").upper() in BLOCKING_STATES:
            reasons.append(key + ":" + str(result[key]))
    for key, value in result.items():
        if (key == "error" or key.endswith("_error")) and value:
            reasons.append(key)
    try:
        weight = multiplier(result.get("trade_ev_evidence_weight"), 0.0)
        if result.get("trade_ev_state") == "NEGATIVE_EV" and weight >= 0.25:
            reasons.append("MATURE_NEGATIVE_EV")
    except (TypeError, ValueError, OverflowError):
        reasons.append("INVALID_EVIDENCE_WEIGHT")
    try:
        original = float(result.get("original_notional", 0.0))
        amount = float(result.get("adjusted_notional", 0.0))
        if not math.isfinite(original) or not math.isfinite(amount) or amount < 0 or amount > original:
            raise ValueError("invalid amount")
        if amount == 0:
            reasons.append("ZERO_NOTIONAL")
    except (TypeError, ValueError, OverflowError):
        reasons.append("INVALID_NOTIONAL")
    result["entry_policy_version"] = ENTRY_POLICY_VERSION
    result["entry_block_reasons"] = sorted(set(reasons))
    result["entry_allowed"] = not reasons
    if reasons:
        result["adjusted_notional"] = 0.0
        result["combined_multiplier"] = 0.0
    return result


def safe_entry_sizing(assess, *args):
    """Last BUY-only boundary, including unexpected assessor exceptions."""
    try:
        result = assess(*args)
        if not isinstance(result, dict):
            raise ValueError("missing sizing result")
        if result.get("entry_policy_version") != ENTRY_POLICY_VERSION:
            raise ValueError("missing entry admission contract")
        return finalize_entry(result)
    except Exception as exc:
        return finalize_entry({"original_notional": 0.0, "adjusted_notional": 0.0,
                               "error": type(exc).__name__})
