from __future__ import annotations

from datetime import datetime, timezone

# New-policy validation boundary. Existing account equity is intentionally not reset;
# research reports can compare trades entered before/after this boundary.
POLICY_EPOCH = "2026-08-30T11:46:00+00:00"
POLICY_VERSION = "EV_REGIME_BATCH_V2"


def epoch_datetime() -> datetime:
    return datetime.fromisoformat(POLICY_EPOCH).astimezone(timezone.utc)


def is_post_epoch(timestamp: str | None) -> bool:
    if not timestamp:
        return False
    try:
        return datetime.fromisoformat(str(timestamp)).astimezone(timezone.utc) >= epoch_datetime()
    except Exception:
        return False


def metadata() -> dict:
    return {
        "policy_version": POLICY_VERSION,
        "policy_epoch": POLICY_EPOCH,
        "simulation_only": True,
        "broker_order_api_calls": 0,
    }
