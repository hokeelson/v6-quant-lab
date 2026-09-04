from __future__ import annotations

import pandas as pd

import src.simulation_engine as se


class FakeDB:
    def __init__(self):
        self._position = None
        self.state_updates = []

    def ensure_accounts(self, initial_equity):
        return []

    def ensure_crypto_master_account(self, initial_equity):
        return {"account_id": "crypto"}

    def account(self, aid):
        return {"account_id": aid, "cash": 100000.0, "initial_equity": 100000.0}

    def marks(self, aid):
        return {}

    def positions(self, aid=None):
        return [self._position] if self._position else []

    def position(self, aid, symbol):
        return self._position

    def pending_order(self, aid, symbol):
        return None

    def decision(self, did):
        return None

    def model(self, market, symbol, horizon):
        conf = {"short": 60.0, "medium": 82.0, "long": 70.0}[horizon]
        return {
            "strategy": horizon,
            "oos_score": conf,
            "train_score": conf,
            "calibration_score": conf,
            "diagnostics": {"stability": conf, "sample": 1.0},
            "params": {},
        }

    def set_last_processed(self, aid, symbol, ts):
        self.state_updates.append((aid, symbol, ts))


class FakeCache:
    def ensure(self, market, symbol, horizon, now=None):
        idx = pd.date_range("2026-09-01", periods=4, freq="h", tz="UTC")
        df = pd.DataFrame(
            {"open": [1, 1, 1, 1], "high": [1, 1, 1, 1], "low": [1, 1, 1, 1], "close": [1, 1, 1, 1]},
            index=idx,
        )
        return {"data": df, "fetched": 0, "api_called": False}

    def closed_only(self, data, market, horizon, now=None):
        return data


def _fake_decision(df, market, horizon, model, equity):
    conf = {"short": 60.0, "medium": 82.0, "long": 70.0}[horizon]
    return {
        "action": "ENTER",
        "confidence": conf,
        "diagnostics": {
            "oos_score": conf,
            "stability": conf,
            "regime_fit": conf / 100.0,
        },
    }


def test_crypto_horizon_arbitration_selects_one_winner_and_advances_others(monkeypatch):
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")
    monkeypatch.setattr(se, "decision_for", _fake_decision)
    monkeypatch.setattr(
        se,
        "HORIZON_SPECS",
        {hz: {"warmup": 1} for hz in ("short", "medium", "long")},
    )

    db = FakeDB()
    lab = se.SimulationLab(db=db, cache=FakeCache(), initial_equity=100000.0)
    winner = lab.select_crypto_horizon("crypto", "BTCUSDT")

    assert winner["horizon"] == "medium"
    advanced = {aid for aid, _, _ in db.state_updates}
    assert advanced == {"crypto_short", "crypto_long"}


def test_crypto_horizon_arbitration_keeps_position_horizon(monkeypatch):
    monkeypatch.setenv("V6_SINGLE_CRYPTO_ACCOUNT", "1")
    monkeypatch.setattr(se, "decision_for", _fake_decision)
    monkeypatch.setattr(
        se,
        "HORIZON_SPECS",
        {hz: {"warmup": 1} for hz in ("short", "medium", "long")},
    )

    db = FakeDB()
    db._position = {"account_id": "crypto", "symbol": "BTCUSDT", "qty": 1.0, "avg_entry": 1.0, "horizon": "long"}
    lab = se.SimulationLab(db=db, cache=FakeCache(), initial_equity=100000.0)
    winner = lab.select_crypto_horizon("crypto", "BTCUSDT")

    assert winner["horizon"] == "long"
