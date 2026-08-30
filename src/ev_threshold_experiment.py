from __future__ import annotations

THRESHOLDS_R = (0.0, 0.10, 0.20, 0.30)


def threshold_bucket(expected_value_r: float) -> dict:
    """Return research-only EV threshold pass/fail diagnostics.

    The experiment intentionally does not place or authorize orders. It lets the
    Shadow research layer compare how stricter minimum EV requirements would have
    filtered candidates.
    """
    ev_r = float(expected_value_r or 0.0)
    passed = {f"ev_r_ge_{t:.2f}": ev_r >= t for t in THRESHOLDS_R}
    strictest = max((t for t in THRESHOLDS_R if ev_r >= t), default=None)
    return {
        "expected_value_r": ev_r,
        "thresholds": list(THRESHOLDS_R),
        "passed": passed,
        "strictest_passed_threshold_r": strictest,
        "simulation_only": True,
        "broker_order_api_calls": 0,
    }


def compare_thresholds(rows: list[dict]) -> dict:
    """Aggregate candidate/trade observations by hypothetical EV_R threshold."""
    out = []
    for threshold in THRESHOLDS_R:
        selected = [r for r in (rows or []) if float(r.get("expected_value_r", 0.0) or 0.0) >= threshold]
        returns = [float(r.get("realized_return", 0.0) or 0.0) for r in selected if r.get("realized_return") is not None]
        out.append({
            "threshold_r": threshold,
            "selected": len(selected),
            "realized_samples": len(returns),
            "average_realized_return": (sum(returns) / len(returns)) if returns else None,
            "positive_realized_rate": (sum(1 for x in returns if x > 0) / len(returns)) if returns else None,
        })
    return {
        "status": "AVAILABLE",
        "simulation_only": True,
        "broker_order_api_calls": 0,
        "thresholds": out,
    }
