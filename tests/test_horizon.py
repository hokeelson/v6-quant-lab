import json
import numpy as np
import pandas as pd

from src.backtest import ExecutionCosts
from src.forward_db import ForwardDB
from src.horizon_db import HorizonDB
from src.horizon import (
    HorizonManager,HorizonConfig,register_three_horizons,params_for_horizon,
    rank_horizons,HORIZON_PROFILES
)

def mk_df(n=320,start="2025-01-01"):
    idx=pd.date_range(start,periods=n,freq="D",tz="UTC")
    close=100*np.exp(np.linspace(0,0.35,n))
    open_=np.r_[close[0],close[:-1]]
    return pd.DataFrame({
        "open":open_,"high":np.maximum(open_,close)*1.003,
        "low":np.minimum(open_,close)*0.997,"close":close,"volume":1_000_000
    },index=idx)

def add_forward_candidate(db,registered_at):
    db.register_candidate({
        "candidate_id":"base1","market":"stock","symbol":"AAA","strategy":"Trend MA",
        "params":{"fast":20,"slow":60},"registered_at":registered_at,"initial_capital":100000,
        "research_grade":70,"evidence_coverage":0.8,
    })

def config():
    return HorizonConfig(
        stock_costs=ExecutionCosts(0,3,2),crypto_costs=ExecutionCosts(10,5,4),
        minimum_warmup_bars=100,
    )

def test_horizon_params_are_distinct():
    base={"fast":20,"slow":60}
    assert params_for_horizon("Trend MA","medium",base)==base
    assert params_for_horizon("Trend MA","short",base)!={"fast":20,"slow":60}
    assert params_for_horizon("Trend MA","long",base)!={"fast":20,"slow":60}


def test_registers_three_independent_sleeves(tmp_path):
    fdb=ForwardDB(tmp_path/"f.sqlite3")
    hdb=HorizonDB(tmp_path/"h.sqlite3")
    add_forward_candidate(fdb,"2026-08-20T00:00:00+00:00")
    out=register_three_horizons(fdb,hdb,100000,registered_at="2026-08-20T01:00:00+00:00")
    assert len(out)==3
    assert {x["horizon"] for x in hdb.sleeves()}=={"short","medium","long"}
    assert len({x["sleeve_id"] for x in hdb.sleeves()})==3


def test_no_pre_registration_horizon_bars(tmp_path):
    fdb=ForwardDB(tmp_path/"f.sqlite3"); hdb=HorizonDB(tmp_path/"h.sqlite3")
    df=mk_df(); reg=df.index[250].isoformat(); add_forward_candidate(fdb,reg)
    register_three_horizons(fdb,hdb,100000,registered_at=reg)
    manager=HorizonManager(hdb,config())
    for sleeve in hdb.sleeves():
        out=manager.process_sleeve(sleeve,df=df,now_iso=(df.index[-1]+pd.Timedelta(days=1)).isoformat())
        assert out["bars_processed"]==len(df.index[df.index>pd.Timestamp(reg)])
        eq=hdb.equity(sleeve["sleeve_id"])
        assert min(pd.to_datetime([x["bar_time"] for x in eq],utc=True))>pd.Timestamp(reg)


def test_short_sleeve_time_exit_occurs(tmp_path):
    fdb=ForwardDB(tmp_path/"f.sqlite3"); hdb=HorizonDB(tmp_path/"h.sqlite3")
    df=mk_df(); reg=df.index[250].isoformat(); add_forward_candidate(fdb,reg)
    register_three_horizons(fdb,hdb,100000,registered_at=reg)
    short=[s for s in hdb.sleeves() if s["horizon"]=="short"][0]
    manager=HorizonManager(hdb,config())
    manager.process_sleeve(short,df=df,now_iso=(df.index[-1]+pd.Timedelta(days=1)).isoformat())
    reasons=[t["reason"] for t in hdb.trades(short["sleeve_id"]) if t["action"]=="SELL"]
    assert "TIME_EXIT" in reasons


def test_rank_horizons_zero_state_has_complete_schema(tmp_path):
    fdb=ForwardDB(tmp_path/"f.sqlite3"); hdb=HorizonDB(tmp_path/"h.sqlite3")
    add_forward_candidate(fdb,"2026-08-20T00:00:00+00:00")
    register_three_horizons(fdb,hdb,100000,registered_at="2026-08-20T01:00:00+00:00")
    r=rank_horizons(hdb)
    assert len(r)==3
    assert set(["evidence","horizon_score","horizon_label"]).issubset(r.columns)
    assert (r["horizon_score"]==0).all()
