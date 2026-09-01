"""Progress telemetry and a shared, bounded worker-stall policy.

Only completed work / phase transitions advance progress. Reading a snapshot or
writing a heartbeat must never make a blocked computation appear productive.
This module does not access market data, accounts, or order APIs.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

PROGRESS_SCHEMA_VERSION = "WORKER_PROGRESS_V1"
NO_PROGRESS_TIMEOUT_SECONDS = 900
LEGACY_CYCLE_TIMEOUT_SECONDS = 900
CYCLE_HARD_LIMIT_SECONDS = 3600

PHASE_LABELS = {
    "PREPARE": "準備本輪分析",
    "UNIVERSE": "更新觀察標的",
    "CALIBRATION": "校準策略模型",
    "SIMULATION": "檢查訊號與模擬帳戶",
    "GOVERNANCE": "驗證候選模型",
    "MODEL_HEALTH": "核對模型完整度",
    "WATCHLIST": "同步即時觀察清單",
    "PORTFOLIO_RISK": "分析投資組合風險",
    "PRETRADE_RISK": "分析進場風險",
    "DATA_QUALITY": "檢查資料品質",
    "COMPLETE": "本輪完成",
    "FAILED": "本輪發生錯誤",
}
PROGRESS_FIELDS = (
    "progress_schema_version", "phase", "phase_started_at", "phase_elapsed_seconds",
    "phase_completed", "phase_total", "progress_unit", "last_progress_at",
    "progress_events", "cycle_elapsed_seconds", "phase_durations_seconds",
    "last_cycle_duration_seconds", "last_cycle_phase_durations_seconds",
    "first_cycle_complete",
)


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def running_progress_problem(raw, now=None, *, hard_limit=CYCLE_HARD_LIMIT_SECONDS):
    """Return a reason to stop a RUNNING cycle, or None while work is progressing.

    Legacy workers retain their previous 15-minute limit. New workers get a
    15-minute *no-progress* budget plus a non-renewable one-hour cycle ceiling.
    Missing, old-cycle, and future-dated progress cannot renew that budget.
    """
    now = now or _utcnow()
    started = _parse_time(raw.get("last_cycle_started_at"))
    if started is None or started > now:
        return "RUNNING cycle missing or invalid last_cycle_started_at"
    cycle_age = (now - started).total_seconds()
    if cycle_age > hard_limit:
        return f"cycle exceeded absolute {hard_limit}s limit ({cycle_age:.1f}s)"
    if raw.get("progress_schema_version") != PROGRESS_SCHEMA_VERSION:
        if cycle_age > LEGACY_CYCLE_TIMEOUT_SECONDS:
            return f"legacy cycle exceeded {LEGACY_CYCLE_TIMEOUT_SECONDS}s ({cycle_age:.1f}s)"
        return None

    progress = _parse_time(raw.get("last_progress_at"))
    if progress is None or progress < started or progress > now:
        return "RUNNING cycle missing or invalid progress timestamp"
    if not isinstance(raw.get("progress_events"), int) or raw["progress_events"] <= 0:
        return "RUNNING cycle missing progress events"
    age = (now - progress).total_seconds()
    if age > NO_PROGRESS_TIMEOUT_SECONDS:
        return f"no work progress for {age:.1f}s (limit {NO_PROGRESS_TIMEOUT_SECONDS}s)"
    return None


def notify_progress(callback, phase, **details):
    """Telemetry failures must not change the outcome of a trading calculation."""
    if callback is not None:
        try:
            callback(phase, **details)
        except Exception as exc:
            # Do not echo exception contents, which may contain credentials.
            print("WORKER_PROGRESS_ERROR", type(exc).__name__, flush=True)


class CycleProgress:
    """Single-writer cycle tracker; the owner synchronizes snapshot readers."""

    def __init__(self, clock=None, wall_clock=None):
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or _utcnow
        self.active = False
        self.started = self.phase_started = None
        self.phase = "PREPARE"
        self.phase_started_at = self.last_progress_at = None
        self.completed = self.total = self.events = 0
        self.unit = None
        self.durations = {}
        self.last_duration = None
        self.last_durations = {}

    def start(self, started_at=None):
        tick = self.clock()
        self.active = True
        self.started = self.phase_started = tick
        self.phase = "PREPARE"
        self.phase_started_at = self.last_progress_at = started_at or self.wall_clock().isoformat()
        self.completed = self.total = 0
        self.events = 1
        self.unit = None
        self.durations = {}

    def report(self, phase, *, completed=0, total=0, unit=None, **_ignored):
        if not self.active:
            return
        if phase not in PHASE_LABELS:
            raise ValueError("unknown progress phase")
        tick = self.clock()
        stamp = self.wall_clock().isoformat()
        if phase != self.phase:
            self.durations[self.phase] = self.durations.get(self.phase, 0.0) + max(0.0, tick - self.phase_started)
            self.phase_started = tick
            self.phase_started_at = stamp
            self.phase = phase
        self.completed = max(0, int(completed))
        self.total = max(self.completed, int(total))
        self.unit = str(unit)[:100] if unit is not None else None
        self.last_progress_at = stamp
        self.events += 1

    def finish(self, failed=False):
        if not self.active:
            return
        tick = self.clock()
        self.durations[self.phase] = self.durations.get(self.phase, 0.0) + max(0.0, tick - self.phase_started)
        self.last_duration = max(0.0, tick - self.started)
        self.last_durations = dict(self.durations)
        self.active = False
        self.phase = "FAILED" if failed else "COMPLETE"
        self.phase_started_at = self.last_progress_at = self.wall_clock().isoformat()
        self.completed = self.total = 0
        self.unit = None
        self.events += 1

    def snapshot(self):
        tick = self.clock()
        durations = dict(self.durations)
        elapsed = max(0.0, tick - self.phase_started) if self.active else 0.0
        if self.active:
            durations[self.phase] = durations.get(self.phase, 0.0) + elapsed
        cycle_elapsed = max(0.0, tick - self.started) if self.active else self.last_duration
        return {
            "progress_schema_version": PROGRESS_SCHEMA_VERSION,
            "phase": self.phase, "phase_started_at": self.phase_started_at,
            "phase_elapsed_seconds": round(elapsed, 3),
            "phase_completed": self.completed, "phase_total": self.total,
            "progress_unit": self.unit, "last_progress_at": self.last_progress_at,
            "progress_events": self.events,
            "cycle_elapsed_seconds": None if cycle_elapsed is None else round(cycle_elapsed, 3),
            "phase_durations_seconds": {k: round(v, 3) for k, v in durations.items()},
            "last_cycle_duration_seconds": None if self.last_duration is None else round(self.last_duration, 3),
            "last_cycle_phase_durations_seconds": {k: round(v, 3) for k, v in self.last_durations.items()},
        }


def public_progress(raw):
    """An explicit allowlist for both public JSON feeds and dashboard telemetry."""
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key in PROGRESS_FIELDS:
        value = raw.get(key)
        if key not in raw:
            continue
        if key in {"phase_durations_seconds", "last_cycle_phase_durations_seconds"}:
            result[key] = {
                phase: round(float(seconds), 3) for phase, seconds in (value.items() if isinstance(value, dict) else [])
                if phase in PHASE_LABELS and isinstance(seconds, (int, float))
                and math.isfinite(seconds) and seconds >= 0
            }
        elif isinstance(value, float) and not math.isfinite(value):
            result[key] = None
        elif value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value[:100] if isinstance(value, str) else value
    return result


def progress_caption(raw):
    """Small shared presentation helper; never labels liveness as readiness."""
    p = public_progress(raw)
    phase = PHASE_LABELS.get(p.get("phase"), "等待進度資料")
    unit = p.get("progress_unit")
    count = f"{p.get('phase_completed', 0)}/{p.get('phase_total')}" if p.get("phase_total") else ""
    seconds = p.get("phase_elapsed_seconds")
    elapsed = f"本階段 {float(seconds):.0f} 秒" if isinstance(seconds, (int, float)) else ""
    return "｜".join(str(x) for x in (phase, unit, count, elapsed) if x)
