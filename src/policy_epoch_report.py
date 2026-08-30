from __future__ import annotations

from collections import defaultdict

from .policy_epoch import POLICY_EPOCH, POLICY_VERSION, is_post_epoch


def _compound(rows: list[dict]) -> float:
    wealth = 1.0
    for row in rows:
        wealth *= max(1e-9, 1.0 + float(row.get("return_pct", 0.0) or 0.0))
    return wealth - 1.0


def policy_epoch_performance(db, trade_limit: int = 10000) -> dict:
    """Compare trades entered before vs after the current policy epoch.

    Uses entry_bar, so a trade opened before the policy boundary is attributed to
    the old policy even if it closes afterward.
    """
    grouped = defaultdict(lambda: {"pre": [], "post": []})
    for trade in db.recent_trades(trade_limit):
        account = str(trade.get("account_id") or "")
        bucket = "post" if is_post_epoch(trade.get("entry_bar")) else "pre"
        grouped[account][bucket].append(trade)

    accounts = []
    for account, buckets in sorted(grouped.items()):
        pre = buckets["pre"]
        post = buckets["post"]
        accounts.append({
            "account_id": account,
            "pre_epoch_closed_trades": len(pre),
            "pre_epoch_compound_return": _compound(pre) if pre else None,
            "post_epoch_closed_trades": len(post),
            "post_epoch_compound_return": _compound(post) if post else None,
            "post_epoch_wins": sum(1 for x in post if float(x.get("realized_pnl", 0.0) or 0.0) > 0),
            "post_epoch_losses": sum(1 for x in post if float(x.get("realized_pnl", 0.0) or 0.0) < 0),
        })

    return {
        "status": "AVAILABLE",
        "policy_version": POLICY_VERSION,
        "policy_epoch": POLICY_EPOCH,
        "simulation_only": True,
        "broker_order_api_calls": 0,
        "accounts": accounts,
        "summary": {
            "post_epoch_closed_trades": sum(x["post_epoch_closed_trades"] for x in accounts),
            "accounts_with_post_epoch_evidence": sum(1 for x in accounts if x["post_epoch_closed_trades"] > 0),
        },
    }
