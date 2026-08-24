from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_events(
  event_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  market TEXT,
  symbol TEXT,
  horizon TEXT,
  strategy TEXT,
  model_signature TEXT,
  arena_id TEXT,
  status TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON ledger_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_pair ON ledger_events(market,symbol,horizon,created_at DESC);

CREATE TABLE IF NOT EXISTS cycle_snapshots(
  cycle_key TEXT PRIMARY KEY,
  status TEXT,
  assets_checked INTEGER,
  bars_processed INTEGER,
  true_errors INTEGER,
  data_quality_status TEXT,
  data_quality_warnings INTEGER,
  data_quality_critical INTEGER,
  concept_drift_pairs INTEGER,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class TrialLedger:
    """Append-only audit ledger for research trials and model-governance events.

    The ledger is intentionally side-channel only: it never changes strategy,
    execution, sizing, promotion, or broker behavior.
    """

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._c() as c:
            c.executescript(SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _put_event(self, event_key: str, source: str, event_type: str, payload: dict,
                   market=None, symbol=None, horizon=None, strategy=None,
                   model_signature=None, arena_id=None, status=None, created_at=None):
        with self._c() as c:
            c.execute("""
              INSERT OR IGNORE INTO ledger_events(
                event_key,source,event_type,market,symbol,horizon,strategy,model_signature,
                arena_id,status,payload_json,created_at
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                str(event_key), str(source), str(event_type), market,
                str(symbol).upper() if symbol else None, horizon, strategy,
                model_signature, arena_id, status, _json(payload or {}), created_at or _now_iso(),
            ))

    def sync_governance(self, governance_path: str):
        path = Path(governance_path)
        if not path.exists():
            return {"events": 0, "research": 0}
        events = research = 0
        src = sqlite3.connect(str(path), timeout=10)
        src.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "governance_events" in tables:
                for r in src.execute("SELECT * FROM governance_events ORDER BY id"):
                    d = dict(r)
                    key = f"governance:{d.get('id')}"
                    detail = {}
                    try:
                        detail = json.loads(d.get("detail_json") or "{}")
                    except Exception:
                        detail = {"raw": d.get("detail_json")}
                    self._put_event(
                        key, "model_governance", d.get("event_type") or "GOVERNANCE_EVENT", detail,
                        d.get("market"), d.get("symbol"), d.get("horizon"),
                        detail.get("challenger_strategy") or detail.get("strategy"),
                        detail.get("challenger_signature") or detail.get("signature"),
                        d.get("arena_id"), detail.get("verdict") or detail.get("status"), d.get("created_at"),
                    )
                    events += 1
            if "research_state" in tables:
                for r in src.execute("SELECT * FROM research_state"):
                    d = dict(r)
                    ts = d.get("last_research_at") or d.get("updated_at")
                    sig = d.get("last_candidate_signature")
                    raw = f"research:{d.get('market')}:{d.get('symbol')}:{d.get('horizon')}:{ts}:{sig}"
                    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    self._put_event(
                        key, "research_state", "RESEARCH_TRIAL", d,
                        d.get("market"), d.get("symbol"), d.get("horizon"),
                        model_signature=sig, status="RECORDED", created_at=ts,
                    )
                    research += 1
        finally:
            src.close()
        return {"events": events, "research": research}

    def sync_worker_cycle(self, worker_status: dict | None, quality_status: dict | None = None):
        worker_status = worker_status or {}
        finished = worker_status.get("last_cycle_finished_at")
        if not finished:
            return False
        quality_status = quality_status or {}
        key = f"cycle:{finished}"
        payload = {"worker": worker_status, "quality": quality_status}
        with self._c() as c:
            c.execute("""
              INSERT OR IGNORE INTO cycle_snapshots(
                cycle_key,status,assets_checked,bars_processed,true_errors,
                data_quality_status,data_quality_warnings,data_quality_critical,
                concept_drift_pairs,payload_json,created_at
              ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                key, worker_status.get("status"), int(worker_status.get("assets_checked", 0) or 0),
                int(worker_status.get("bars_processed", 0) or 0), int(worker_status.get("true_errors", 0) or 0),
                quality_status.get("status") or worker_status.get("data_quality"),
                int(worker_status.get("data_quality_warnings", 0) or 0),
                int(worker_status.get("data_quality_critical", 0) or 0),
                int(worker_status.get("concept_drift_pairs", 0) or 0),
                _json(payload), finished,
            ))
        return True

    def summary(self):
        with self._c() as c:
            total = c.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
            trials = c.execute("SELECT COUNT(*) FROM ledger_events WHERE event_type='RESEARCH_TRIAL'").fetchone()[0]
            hypotheses = c.execute("SELECT COUNT(DISTINCT model_signature) FROM ledger_events WHERE model_signature IS NOT NULL AND model_signature<>''").fetchone()[0]
            challenges = c.execute("SELECT COUNT(*) FROM ledger_events WHERE event_type='CHALLENGE_REGISTERED'").fetchone()[0]
            promotions = c.execute("SELECT COUNT(*) FROM ledger_events WHERE event_type LIKE '%PROMOT%'").fetchone()[0]
            cycles = c.execute("SELECT COUNT(*) FROM cycle_snapshots").fetchone()[0]
            last = c.execute("SELECT created_at FROM ledger_events ORDER BY created_at DESC LIMIT 1").fetchone()
        return {
            "events": int(total), "research_trials": int(trials), "distinct_hypotheses": int(hypotheses),
            "challenges": int(challenges), "promotions": int(promotions), "cycles": int(cycles),
            "last_event_at": last[0] if last else None,
        }

    def recent_events(self, limit=300):
        with self._c() as c:
            rows = c.execute("SELECT * FROM ledger_events ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def recent_cycles(self, limit=100):
        with self._c() as c:
            rows = c.execute("SELECT * FROM cycle_snapshots ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
