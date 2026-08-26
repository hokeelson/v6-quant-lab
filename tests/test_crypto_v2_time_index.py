from __future__ import annotations

import numpy as np
import pandas as pd

from src.crypto_v2.shadow_engine import _through


def test_through_normalizes_object_like_time_index():
    idx = np.array([
        "2026-08-26T00:00:00+00:00",
        "2026-08-26T01:00:00+00:00",
        "2026-08-26T02:00:00+00:00",
    ], dtype=object)
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=pd.Index(idx, dtype=object))
    out = _through(df, pd.Timestamp("2026-08-26T01:00:00Z"))
    assert len(out) == 2
    assert out["close"].tolist() == [1.0, 2.0]
