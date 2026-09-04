from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dashboard_v9 as d


class FakeRealtimeDB:
    def __init__(self, path=None):
        pass

    def quotes(self):
        now = datetime.now(timezone.utc)
        return [
            {"market": "crypto", "symbol": "BTCUSDT", "price": 123.45, "ts": now.isoformat(), "source": "STREAM"},
            {"market": "crypto", "symbol": "ETHUSDT", "price": 999.0, "ts": (now - timedelta(seconds=120)).isoformat(), "source": "STREAM"},
            {"market": "stock", "symbol": "AAPL", "price": 200.0, "ts": now.isoformat(), "source": "STREAM"},
        ]


def test_fresh_quote_map_uses_only_fresh_crypto(monkeypatch):
    monkeypatch.setattr(d, "RealtimeDB", FakeRealtimeDB)
    monkeypatch.setattr(d, "db_path", lambda name: "/tmp/fake.sqlite3")

    quotes = d._fresh_quote_map(max_age_seconds=60)

    assert set(quotes) == {"BTCUSDT"}
    assert quotes["BTCUSDT"]["price"] == 123.45
    assert quotes["BTCUSDT"]["source"] == "STREAM"
