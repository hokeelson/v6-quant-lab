from __future__ import annotations

import json
from datetime import datetime, timezone

from src.binance_market_context import binance_market_context_assessment


def test_binance_context_assessment_reads_fresh_snapshot(tmp_path):
    path = tmp_path / "context.json"
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "AVAILABLE",
        "rows": [{
            "symbol": "BTCUSDT",
            "spot_depth_available": True,
            "size_multiplier": 0.70,
            "risk_score": 0.55,
            "risk_state": "CAUTION",
            "reasons": ["positioning_crowded"],
            "last_funding_rate": 0.001,
            "open_interest": 123.0,
            "long_short_ratio": 2.1,
            "bid_share_top20": 0.72,
            "spread_bps": 1.2,
        }],
    }), encoding="utf-8")
    row = binance_market_context_assessment("BTCUSDT", path)
    assert row["binance_context_status"] == "AVAILABLE"
    assert row["binance_context_multiplier"] == 0.70
    assert row["binance_long_short_ratio"] == 2.1


def test_local_launcher_runs_binance_context_worker():
    text = open("local_crypto_lite.py", encoding="utf-8").read()
    assert "binance_market_context_worker.py" in text
    assert "V6_ALLOW_PAPER_ORDERS" in text
