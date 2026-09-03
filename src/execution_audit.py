"""Bounded, read-only execution evidence and recorded P&L attribution.

Never infer legacy entry IDs from matching symbols, dates or prices. This module
is an observer: it cannot place orders, repair rows, or change risk parameters.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .entry_gate import ENTRY_POLICY_VERSION


def _same_number(a, b):
    try:
        return math.isfinite(float(a)) and math.isfinite(float(b)) and math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=1e-8)
    except (ValueError, TypeError):
        return False


def _stamp(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _trace(con, limit, deadline):
    orders = [dict(r) for r in con.execute(
        "SELECT * FROM orders WHERE side='BUY' ORDER BY created_at DESC,order_id DESC LIMIT ?", (limit,))]
    total = con.execute("SELECT COUNT(*) FROM orders WHERE side='BUY'").fetchone()[0]
    diagnostics = con.execute("SELECT id,account_id,symbol,category,payload_json,created_at FROM diagnostics "
                              "WHERE category IN ('RISK_SIZING','ORDER_CANCELLED') ORDER BY id DESC LIMIT 5000").fetchall()
    gates = defaultdict(list)
    malformed = 0
    for r in diagnostics:
        if time.monotonic() > deadline:
            raise TimeoutError("audit budget")
        try:
            p = json.loads(r["payload_json"] or "{}")
            if not isinstance(p, dict):
                raise ValueError("non-object")
        except (ValueError, TypeError):
            malformed += 1
            continue
        if p.get("entry_policy_version") == ENTRY_POLICY_VERSION and p.get("order_id"):
            gates[p["order_id"]].append({"id": r["id"], "account_id": r["account_id"],
                                        "symbol": r["symbol"], "created_at": r["created_at"],
                                        "decision_id": p.get("decision_id"),
                                        "allowed": p.get("entry_allowed"),
                                        "filled_notional": p.get("filled_notional"),
                                        "reasons": p.get("entry_block_reasons", [])})
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    pcols = {r[1] for r in con.execute("PRAGMA table_info(positions)")}
    rows = []
    for o in orders:
        if time.monotonic() > deadline:
            raise TimeoutError("audit budget")
        item = {k: o[k] for k in ("order_id", "decision_id", "account_id", "symbol", "created_at", "filled_bar", "fill_price")}
        item.update(order_status=o["status"], status="UNRESOLVED", issues=[], trade_ids=[])
        g = gates.get(o["order_id"], [])
        if len(g) != 1:
            item["issues"].append("MISSING_OR_AMBIGUOUS_GATE_LINK")
            rows.append(item)
            continue
        g = g[0]
        item.update(gate_event_id=g["id"], gate_created_at=g["created_at"], entry_allowed=g["allowed"],
                    block_reasons=g["reasons"] if isinstance(g["reasons"], list) else [])
        if g["account_id"] != o["account_id"] or g["symbol"] != o["symbol"] or g["decision_id"] != o["decision_id"]:
            item.update(status="ERROR", issues=["GATE_IDENTITY_MISMATCH"])
        elif g["allowed"] is False:
            item["status"] = "ERROR" if o["status"] == "FILLED" else "VALIDATED_BLOCKED" if o["status"] == "CANCELLED" else "UNRESOLVED"
            if o["status"] == "FILLED":
                item["issues"].append("BLOCKED_BUT_FILLED")
        elif g["allowed"] is not True:
            item["issues"].append("INVALID_GATE_BOOLEAN")
        elif o["status"] != "FILLED":
            item["status"] = "NOT_FILLED"
        else:
            d = con.execute("SELECT * FROM decisions WHERE decision_id=?", (o["decision_id"],)).fetchone()
            if not d or d["account_id"] != o["account_id"] or d["symbol"] != o["symbol"] or d["action"] != "ENTER":
                item["issues"].append("MISSING_OR_MISMATCHED_DECISION")
            elif not _stamp(d["bar_time"]) or not _stamp(o["filled_bar"]) or _stamp(d["bar_time"]) >= _stamp(o["filled_bar"]):
                item["issues"].append("INVALID_DECISION_FILL_SEQUENCE")
            trades = con.execute("SELECT * FROM trades WHERE entry_order_id=?", (o["order_id"],)).fetchall() if "entry_order_id" in cols else []
            positions = con.execute("SELECT * FROM positions WHERE entry_order_id=?", (o["order_id"],)).fetchall() if "entry_order_id" in pcols else []
            if len(trades) + len(positions) != 1:
                item["issues"].append("MISSING_OR_AMBIGUOUS_POSITION_TRADE_LINK")
            else:
                linked = dict((trades or positions)[0])
                price = linked["entry_price"] if trades else linked["avg_entry"]
                if (linked["account_id"] != o["account_id"] or linked["symbol"] != o["symbol"]
                        or _stamp(linked["entry_bar"]) != _stamp(o["filled_bar"])
                        or not _same_number(price, o["fill_price"])
                        or not _same_number(linked["qty"] * price, g["filled_notional"])):
                    item["issues"].append("ENTRY_LINK_VALUE_MISMATCH")
                item["qty"] = linked["qty"]
                if trades:
                    item.update(trade_ids=[linked["trade_id"]], exit_bar=linked["exit_bar"],
                                exit_reason=linked["exit_reason"], exit_order_id=linked["exit_order_id"],
                                realized_pnl=linked["realized_pnl"])
                    if not _stamp(linked["exit_bar"]) or not _stamp(linked["entry_bar"]) or _stamp(linked["exit_bar"]) < _stamp(linked["entry_bar"]):
                        item["issues"].append("INVALID_EXIT_SEQUENCE")
                    if not _same_number(linked["realized_pnl"], linked["qty"] * (linked["exit_price"] - linked["entry_price"])):
                        item["issues"].append("PNL_MISMATCH")
                    if linked["exit_order_id"]:
                        ex = con.execute("SELECT * FROM orders WHERE order_id=?", (linked["exit_order_id"],)).fetchone()
                        if (not ex or ex["side"] != "SELL" or ex["status"] != "FILLED" or ex["account_id"] != o["account_id"]
                                or ex["symbol"] != o["symbol"] or _stamp(ex["filled_bar"]) != _stamp(linked["exit_bar"])
                                or not _same_number(ex["fill_price"], linked["exit_price"])
                                or not _same_number(ex["qty"], linked["qty"])):
                            item["issues"].append("EXIT_ORDER_MISMATCH")
                    elif linked["exit_reason"] not in {"ATR_STOP", "ATR_STOP_GAP", "ATR_TARGET", "TIME_EXIT", "MARGIN_LIQUIDATION"}:
                        item["issues"].append("UNSUPPORTED_ORDERLESS_EXIT")
                if not item["issues"]:
                    item["status"] = "VALIDATED_CLOSED" if trades else "VALIDATED_OPEN"
            if item["issues"]:
                item["status"] = "UNRESOLVED" if all(x.startswith("MISSING") for x in item["issues"]) else "ERROR"
        rows.append(item)
    return {"status": "ERROR" if any(x["status"] == "ERROR" for x in rows) else "PARTIAL" if any(x["status"] == "UNRESOLVED" for x in rows) else "AVAILABLE" if rows else "WAITING_FOR_EVENTS",
            "summary": dict(Counter(x["status"] for x in rows)), "entries": rows,
            "coverage": {"sampled_buy_orders": len(rows), "total_buy_orders": total, "order_limit": limit,
                         "diagnostic_limit": 5000, "diagnostics_scanned": len(diagnostics), "malformed_diagnostics": malformed,
                         "legacy_links_inferred": False},
            "note": "VALIDATED_OPEN is not a completed round trip; legacy or missing gate IDs remain UNRESOLVED."}


def _pnl(con):
    groups = {}
    for name, keys in (("by_account", "account_id"), ("by_strategy", "account_id,strategy"),
                       ("by_symbol", "account_id,symbol"), ("by_exit_reason", "account_id,exit_reason")):
        rows = con.execute(f"SELECT {keys},COUNT(*) AS closed_trades,SUM(realized_pnl) AS realized_pnl,"
                           "SUM(CASE WHEN realized_pnl<0 THEN realized_pnl ELSE 0 END) AS losing_pnl,"
                           "SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END) AS winning_pnl,"
                           f"MIN(exit_bar) AS first_exit_bar,MAX(exit_bar) AS last_exit_bar FROM trades GROUP BY {keys} "
                           "ORDER BY realized_pnl ASC LIMIT 50").fetchall()
        groups[name] = [dict(r) for r in rows]
    return {"status": "AVAILABLE", "scope": "ALL_RECORDED_CLOSED_TRADES", "groups": groups,
            "group_limit": 50, "ordering": "worst_recorded_pnl_first",
            "note": "Recorded realized P&L uses cost-adjusted entry/exit prices. Do not subtract order fees/slippage again. "
                    "Financing and open-position P&L are excluded; this is not daily return, a causal explanation, or total account return. "
                    "Accounts are kept separate; no cross-currency sum is inferred."}


def execution_audit(path, limit=200, budget_seconds=8):
    out = {"schema_version": "EXECUTION_AUDIT_V1", "generated_at": datetime.now(timezone.utc).isoformat(),
           "status": "UNAVAILABLE", "contains_secrets": False, "scope": "PUBLIC_READ_ONLY_EXECUTION_AUDIT"}
    path = Path(path)
    if not path.is_file():
        return {**out, "error": "DATABASE_MISSING"}
    con = None
    deadline = time.monotonic() + max(0.01, min(float(budget_seconds), 15))
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=min(1, max(.01, budget_seconds)))
        con.row_factory = sqlite3.Row
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        trace = _trace(con, max(1, min(int(limit), 500)), deadline)
        pnl = _pnl(con)
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("accounts", "decisions", "orders", "positions", "trades")}
        return {**out, "status": trace["status"], "lifecycle": trace, "pnl_attribution": pnl, "ledger_counts": counts}
    except (sqlite3.Error, ValueError, TypeError, OverflowError, TimeoutError) as exc:
        return {**out, "status": "ERROR", "error": type(exc).__name__}
    finally:
        if con is not None:
            con.close()
