import json
import numpy as np
import pandas as pd

from src.simulation_db import SimulationDB
from src.decision_engine import market_regime, decision_for


def sample_df(n=350):
    idx=pd.date_range("2024-01-01",periods=n,freq="D",tz="UTC")
    close=pd.Series(np.linspace(100,150,n)+np.sin(np.arange(n)/7),index=idx)
    return pd.DataFrame({
        "open":close.shift(1).fillna(close.iloc[0]),
        "high":close*1.01,
        "low":close*0.99,
        "close":close,
        "volume":1_000_000+np.arange(n)*100,
    },index=idx)


def test_six_equal_accounts(tmp_path):
    db=SimulationDB(str(tmp_path/"sim.sqlite3"))
    rows=db.ensure_accounts(100000)
    assert len(rows)==6
    assert {r["initial_equity"] for r in rows}=={100000.0}
    assert {r["cash"] for r in rows}=={100000.0}


def test_stage6_does_not_need_broker_credentials(tmp_path):
    db=SimulationDB(str(tmp_path/"sim.sqlite3"))
    db.ensure_accounts(100000)
    db.add_asset("stock","AAPL")
    assert db.assets()[0]["symbol"]=="AAPL"


def test_decision_confidence_gate_and_atr_sizing():
    df=sample_df()
    model={
        "strategy":"Trend MA","params":{"fast":10,"slow":60},
        "oos_score":85.0,"train_score":82.0,"calibration_score":84.0,
        "diagnostics":{"stability":90.0},
    }
    d=decision_for(df,"stock","long",model,100000)
    assert d["stop_distance"]>0
    assert d["target_distance"]>d["stop_distance"]
    assert 1.0 <= d["leverage"] <= 1.5
    assert d["requested_notional"]>0


def test_low_evidence_can_be_no_trade():
    df=sample_df()
    model={
        "strategy":"Trend MA","params":{"fast":10,"slow":60},
        "oos_score":10.0,"train_score":15.0,"calibration_score":12.0,
        "diagnostics":{"stability":20.0},
    }
    d=decision_for(df,"stock","long",model,100000)
    assert d["action"] in {"NO_TRADE","EXIT"}


def test_decision_roundtrip(tmp_path):
    db=SimulationDB(str(tmp_path/"sim.sqlite3")); db.ensure_accounts(100000)
    did=db.add_decision({
        "account_id":"stock_short","market":"stock","symbol":"AAPL","horizon":"short",
        "bar_time":"2026-08-20T14:00:00+00:00","action":"ENTER","confidence":80.0,
        "strategy":"Momentum","params":{"lookback":12,"threshold":0.01},"regime":"NORMAL_UP_TREND",
        "atr_pct":0.02,"stop_distance":0.034,"target_distance":0.056,"risk_budget_pct":0.005,
        "requested_notional":15000,"leverage":1.2,"reason":"qualified_signal",
        "diagnostics":{"max_holding_bars":36}
    })
    d=db.decision(did)
    assert d["params"]["lookback"]==12
    assert d["diagnostics"]["max_holding_bars"]==36
