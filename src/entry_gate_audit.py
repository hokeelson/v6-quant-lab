"""Read-only public audit of version-tagged admission decisions."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .entry_gate import ENTRY_POLICY_VERSION


def entry_gate_audit(path, limit=200):
    out = {"policy_version": ENTRY_POLICY_VERSION, "status": "WAITING_FOR_EVENTS",
           "simulation_only": True, "broker_order_api_calls": 0, "entries": [],
           "summary": {"sampled_events": 0, "blocked": 0, "filled": 0,
                       "blocked_but_filled": 0}}
    if not Path(path).is_file():
        return out
    try:
        with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id,account_id,symbol,created_at,category,payload_json FROM diagnostics "
                "WHERE category IN ('RISK_SIZING','ORDER_CANCELLED') AND payload_json LIKE ? "
                "ORDER BY id DESC LIMIT ?", ("%" + ENTRY_POLICY_VERSION + "%", int(limit))
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("entry_policy_version") != ENTRY_POLICY_VERSION:
                continue
            allowed = payload.get("entry_allowed") is True
            filled = row["category"] == "RISK_SIZING" and float(payload.get("filled_notional") or 0) > 0
            out["entries"].append({
                "id": row["id"], "account_id": row["account_id"], "symbol": row["symbol"],
                "order_id": payload.get("order_id"), "decision_id": payload.get("decision_id"),
                "created_at": row["created_at"], "entry_allowed": allowed, "filled": filled,
                "reasons": payload.get("entry_block_reasons", []),
                "filled_notional": payload.get("filled_notional", 0.0),
            })
            out["summary"]["blocked"] += int(not allowed)
            out["summary"]["filled"] += int(filled)
            out["summary"]["blocked_but_filled"] += int(not allowed and filled)
        out["summary"]["sampled_events"] = len(out["entries"])
        out["status"] = ("ERROR" if out["summary"]["blocked_but_filled"]
                         else "OK" if out["entries"] else "WAITING_FOR_EVENTS")
    except Exception as exc:
        out["status"] = "ERROR"
        out["error"] = type(exc).__name__
    return out
