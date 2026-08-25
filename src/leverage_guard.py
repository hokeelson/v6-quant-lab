from __future__ import annotations

import math


def cost_aware_leverage_room(equity: float, gross: float, max_leverage: float, one_way_rate: float,
                             headroom_ratio: float = 0.005) -> dict:
    """Return a conservative BUY-notional cap after accounting for entry cost drag."""
    e = float(equity or 0.0)
    g = max(0.0, float(gross or 0.0))
    L = max(0.0, float(max_leverage or 0.0))
    r = max(0.0, float(one_way_rate or 0.0))
    h = max(0.0, min(0.05, float(headroom_ratio or 0.0)))

    legacy_room = max(0.0, e * L - g) if e > 0 and L > 0 else 0.0
    target = L * (1.0 - h)
    if e <= 0 or target <= 0:
        return {
            "legacy_room": legacy_room,
            "cost_adjusted_room": 0.0,
            "max_leverage": L,
            "target_leverage_cap": target,
            "headroom_ratio": h,
            "mark_fraction": 0.0,
            "cost_drag_fraction": 1.0,
        }

    mark_fraction = 1.0 / (1.0 + r)
    cost_drag = 1.0 - mark_fraction
    denom = mark_fraction + target * cost_drag
    numer = target * e - g
    room = max(0.0, numer / denom) if denom > 0 else 0.0
    room = min(legacy_room, room)

    return {
        "legacy_room": legacy_room,
        "cost_adjusted_room": room,
        "max_leverage": L,
        "target_leverage_cap": target,
        "headroom_ratio": h,
        "mark_fraction": mark_fraction,
        "cost_drag_fraction": cost_drag,
    }


def projected_post_fill(equity: float, gross: float, notional: float, one_way_rate: float) -> dict:
    e = float(equity or 0.0)
    g = max(0.0, float(gross or 0.0))
    n = max(0.0, float(notional or 0.0))
    r = max(0.0, float(one_way_rate or 0.0))
    mark_fraction = 1.0 / (1.0 + r)
    new_mark = n * mark_fraction
    projected_gross = g + new_mark
    projected_equity = e - n + new_mark
    projected_leverage = projected_gross / projected_equity if projected_equity > 0 else math.inf
    return {
        "projected_post_fill_gross": projected_gross,
        "projected_post_fill_equity": projected_equity,
        "projected_post_fill_leverage": projected_leverage,
    }
