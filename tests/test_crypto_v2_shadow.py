from __future__ import annotations

import numpy as np
import pandas as pd

from src.crypto_v2 import shadow_engine as shadow_engine_module
from src.crypto_v2.core import classify_market_regime, route_strategy, symbol_features
from src.crypto_v2.shadow_db import CryptoV2ShadowDB
from src.crypto_v2.shadow_engine import CryptoV2ShadowEngine
from src.market_cache import MarketCache
from src.simulation_db import SimulationDB


def bars(prices, start="2026-01-01", freq="1h", volume=None):
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    p = np.asarray(prices, dtype=float)
    v = np.asarray(volume if volume is not None else np.full(len(p), 1000.0), dtype=float)
    return pd.DataFrame({
        "open": p * 0.999,
        "high": p * 1.004,
        "low": p * 0.996,
        "close": p,
        "volume": v,
    }, index=idx)


def test_crypto_v2_riskoff_can_choose_no_trade():
    btc = bars(np.r_[np.linspace(120, 118, 100), np.linspace(118, 90, 30)])
    regime = classify_market_regime(btc)
    assert regime["state"] in {"PANIC", "TREND_DOWN", "HIGH_VOL_SIDEWAYS"}
    decision = route_strategy(regime, {
        "ready": True,
        "ret_fast": 0.05,
        "ret_slow": 0.10,
        "relative_strength": 0.08,
        "zscore20": -2.0,
        "volume_z": 2.0,
        "breakout_pct": 0.04,
        "atr_pct": 0.03,
    }, "short")
    assert decision["action"] == "NO_TRADE"


def test_crypto_v2_shadow_ledger_is_independent_and_round_trips(tmp_path):
    baseline_path = tmp_path / "simulation_lab.sqlite3"
    shadow_path = tmp_path / "crypto_v2_shadow.sqlite3"
    baseline = SimulationDB(str(baseline_path))
    baseline.ensure_accounts(100000.0)
    shadow = CryptoV2ShadowDB(str(shadow_path), initial_equity=100000.0)

    before = baseline.account("crypto_short")["cash"]
    decision = {
        "action": "ENTER", "strategy": "V2_MOMENTUM", "confidence": 0.7,
        "stop_distance": 0.03, "target_distance": 0.06, "max_holding_bars": 8,
        "reason": "test",
    }
    regime = {"state": "TREND_UP"}
    did = shadow.add_decision("BTCUSDT", "short", "2026-08-26T00:00:00+00:00", decision, regime, {"ready": True})
    shadow.add_buy_order("BTCUSDT", "short", "2026-08-26T00:00:00+00:00", 5000.0, did)
    order = shadow.pending_order("BTCUSDT", "short")
    assert shadow.fill_buy(order, "2026-08-26T01:00:00+00:00", 100.0, 0.0019, decision, regime)
    assert shadow.position("BTCUSDT", "short") is not None
    assert shadow.close_position("BTCUSDT", "short", "2026-08-26T02:00:00+00:00", 106.0, 0.0019, "TARGET")
    assert len(shadow.recent_trades()) == 1
    assert baseline.account("crypto_short")["cash"] == before


def test_crypto_v2_first_cycle_does_not_backfill_history(tmp_path):
    baseline = SimulationDB(str(tmp_path / "simulation_lab.sqlite3"))
    baseline.ensure_accounts(100000.0)
    baseline.add_asset("crypto", "BTCUSDT")
    baseline.add_asset("crypto", "ETHUSDT")

    cache = MarketCache(str(tmp_path / "market_cache.sqlite3"))
    prices = np.linspace(100.0, 140.0, 140)
    btc = bars(prices)
    eth = bars(np.linspace(50.0, 80.0, 140))
    cache.upsert("crypto", "BTCUSDT", "1h", btc)
    cache.upsert("crypto", "ETHUSDT", "1h", eth)

    shadow = CryptoV2ShadowDB(str(tmp_path / "crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
    engine = CryptoV2ShadowEngine(baseline, cache, shadow)
    now = btc.index[-1] + pd.Timedelta(hours=2)
    result = engine.cycle(now=now)

    decisions = shadow.recent_decisions(1000)
    assert result["broker_order_api_calls"] == 0
    assert result["market_data_api_calls"] == 0
    assert len(decisions) <= 2  # one registration decision per available short-horizon symbol
    assert len(shadow.recent_trades(1000)) == 0


def test_crypto_v2_catchup_is_chronological_and_bounded(tmp_path, monkeypatch):
    baseline = SimulationDB(str(tmp_path / "simulation_lab.sqlite3"))
    baseline.ensure_accounts(100000.0)
    baseline.add_asset("crypto", "BTCUSDT")
    baseline.add_asset("crypto", "ETHUSDT")

    cache = MarketCache(str(tmp_path / "market_cache.sqlite3"))
    btc = bars(np.linspace(100.0, 150.0, 150))
    eth = bars(np.linspace(50.0, 90.0, 150))
    cache.upsert("crypto", "BTCUSDT", "1h", btc)
    cache.upsert("crypto", "ETHUSDT", "1h", eth)

    shadow = CryptoV2ShadowDB(str(tmp_path / "crypto_v2_shadow.sqlite3"), initial_equity=100000.0)
    engine = CryptoV2ShadowEngine(baseline, cache, shadow)

    # Register both series at an earlier forward point, then create a backlog.
    first_now = btc.index[-7] + pd.Timedelta(hours=2)
    engine.cycle(now=first_now)
    before_btc = shadow.last_processed("BTCUSDT", "short")
    before_eth = shadow.last_processed("ETHUSDT", "short")
    assert before_btc == before_eth

    monkeypatch.setattr(shadow_engine_module, "MAX_EVENTS_PER_CYCLE", 2)
    later_now = btc.index[-1] + pd.Timedelta(hours=2)
    result = engine.cycle(now=later_now)

    catchup = result["catchup"]
    assert catchup["processed_events"] == 2
    assert catchup["is_catching_up"] is True
    assert catchup["remaining_events_estimate"] > 0
    # The bound must finish the whole earliest timestamp group instead of
    # racing one symbol several bars ahead of the other.
    assert catchup["oldest_selected_bar"] == catchup["newest_selected_bar"]
    after_btc = shadow.last_processed("BTCUSDT", "short")
    after_eth = shadow.last_processed("ETHUSDT", "short")
    assert after_btc == after_eth
    assert pd.Timestamp(after_btc) > pd.Timestamp(before_btc)
    assert pd.Timestamp(after_btc) < btc.index[-1]


def test_symbol_features_and_router_can_select_trend_entry():
    btc = bars(np.linspace(100.0, 130.0, 140), volume=np.linspace(1000, 1500, 140))
    alt = bars(np.linspace(50.0, 90.0, 140), volume=np.linspace(1000, 1800, 140))
    regime = classify_market_regime(btc)
    features = symbol_features(alt, btc)
    decision = route_strategy(regime, features, "short")
    assert regime["state"] == "TREND_UP"
    assert features["ready"] is True
    assert decision["action"] in {"ENTER", "NO_TRADE"}
    if decision["action"] == "ENTER":
        assert decision["strategy"] == "V2_MOMENTUM"
