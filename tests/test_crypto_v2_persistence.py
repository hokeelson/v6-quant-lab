from __future__ import annotations

import sqlite3

from src.crypto_v2.persistence import checkpoint_shadow_db
from src.crypto_v2.shadow_db import CryptoV2ShadowDB


def test_crypto_v2_checkpoint_persists_open_position(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    persist = tmp_path / "persist"
    runtime.mkdir()
    monkeypatch.setenv("V6_PERSISTENT_DATA_DIR", str(persist))

    db_path = runtime / "crypto_v2_shadow.sqlite3"
    db = CryptoV2ShadowDB(str(db_path), initial_equity=100000.0)
    decision = {
        "action": "ENTER",
        "strategy": "V2_MEAN_REVERSION",
        "confidence": 0.7,
        "stop_distance": 0.03,
        "target_distance": 0.06,
        "max_holding_bars": 8,
        "reason": "test",
    }
    regime = {"state": "SIDEWAYS"}
    did = db.add_decision(
        "BTCUSDT",
        "short",
        "2026-08-26T00:00:00+00:00",
        decision,
        regime,
        {"ready": True},
    )
    db.add_buy_order("BTCUSDT", "short", "2026-08-26T00:00:00+00:00", 5000.0, did)
    order = db.pending_order("BTCUSDT", "short")
    assert db.fill_buy(order, "2026-08-26T01:00:00+00:00", 100.0, 0.0019, decision, regime)
    db.set_last_processed("BTCUSDT", "short", "2026-08-26T01:00:00+00:00")

    assert checkpoint_shadow_db(str(db_path)) is True
    saved = persist / "v6-snapshots" / "current" / "crypto_v2_shadow.sqlite3"
    assert saved.exists()

    con = sqlite3.connect(saved)
    try:
        pos = con.execute(
            "SELECT symbol,horizon FROM positions WHERE symbol='BTCUSDT' AND horizon='short'"
        ).fetchone()
        state = con.execute(
            "SELECT last_processed_bar FROM engine_state WHERE symbol='BTCUSDT' AND horizon='short'"
        ).fetchone()
    finally:
        con.close()

    assert pos == ("BTCUSDT", "short")
    assert state == ("2026-08-26T01:00:00+00:00",)
