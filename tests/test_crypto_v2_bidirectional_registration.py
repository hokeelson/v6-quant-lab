from __future__ import annotations

from src.crypto_v2.bidirectional_registration import (
    is_forward_eligible,
    registration_state,
)
from src.crypto_v2.research import ResearchCryptoV2ShadowDB


def test_registration_blocks_pre_registration_backfill_and_persists(tmp_path):
    db = ResearchCryptoV2ShadowDB(str(tmp_path / "v2.sqlite3"), initial_equity=100000.0)
    state1 = registration_state(db)
    state2 = registration_state(db)

    assert state1["registered_at"] == state2["registered_at"]
    assert state1["historical_backfill_allowed"] is False
    assert is_forward_eligible(db, "2020-01-01T00:00:00+00:00") is False
    assert is_forward_eligible(db, "2099-01-01T00:00:00+00:00") is True
