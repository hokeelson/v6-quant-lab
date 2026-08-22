import json
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path

from src.forward_db import ForwardDB
from src.forward import (
    ForwardManager, ForwardConfig, candidate_id, rank_forward, promotion_decision
)
from src.backtest import ExecutionCosts, RiskRules

def mk_df(n=280, start="2025-01-01"):
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    close = 100*np.exp(np.linspace(0,0.30,n))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "open":open_,"high":np.maximum(open_,close)*1.005,
        "low":np.minimum(open_,close)*0.995,"close":close,"volume":1_000_000
    }, index=idx)

def cfg():
    return ForwardConfig(
        stock_costs=ExecutionCosts(0,3,2),
        crypto_costs=ExecutionCosts(10,5,4),
        risk=RiskRules(max_position_pct=.2, stop_loss_pct=.08, take_profit_pct=.20),
        minimum_warmup_bars=100,
    )

def register(db, registered_at, params=None):
    params = params or {"fast":10,"slow":30}
    cid = candidate_id("stock","AAA","Trend MA",params,registered_at)
    db.register_candidate({
        "candidate_id":cid,"market":"stock","symbol":"AAA","strategy":"Trend MA",
        "params":params,"registered_at":registered_at,"initial_capital":100000,
        "research_grade":80,"evidence_coverage":1.0,
    })
    return cid

def test_forward_never_counts_pre_registration_bars(tmp_path):
    db = ForwardDB(tmp_path/"f.sqlite3")
    df = mk_df()
    reg = df.index[200].isoformat()
    cid = register(db, reg)
    m = ForwardManager(db,cfg())
    c = db.candidates()[0]
    out = m.process_candidate(c, df=df, now_iso=(df.index[-1] + pd.Timedelta(days=1)).isoformat())
    eq = db.equity(cid)
    assert out["bars_processed"] == len(df.index[df.index > pd.Timestamp(reg)])
    assert min(pd.to_datetime([r["bar_time"] for r in eq], utc=True)) > pd.Timestamp(reg)

def test_forward_is_idempotent(tmp_path):
    db = ForwardDB(tmp_path/"f.sqlite3")
    df = mk_df()
    reg = df.index[200].isoformat()
    cid = register(db, reg)
    m = ForwardManager(db,cfg())
    c = db.candidates()[0]
    first = m.process_candidate(c, df=df, now_iso=(df.index[-1] + pd.Timedelta(days=1)).isoformat())
    trades1 = len(db.trades(cid))
    eq1 = len(db.equity(cid))
    second = m.process_candidate(c, df=df, now_iso=(df.index[-1] + pd.Timedelta(days=1)).isoformat())
    assert first["bars_processed"] > 0
    assert second["bars_processed"] == 0
    assert len(db.trades(cid)) == trades1
    assert len(db.equity(cid)) == eq1

def test_signal_executes_on_later_bar(tmp_path):
    db = ForwardDB(tmp_path/"f.sqlite3")
    df = mk_df()
    reg = df.index[200].isoformat()
    cid = register(db, reg, {"fast":2,"slow":3})
    m = ForwardManager(db,cfg())
    c = db.candidates()[0]
    m.process_candidate(c, df=df, now_iso=(df.index[-1] + pd.Timedelta(days=1)).isoformat())
    trades = db.trades(cid)
    assert trades
    first = trades[0]
    if first["signal_bar"] is not None:
        assert pd.Timestamp(first["bar_time"]) > pd.Timestamp(first["signal_bar"])

def test_promotion_gate_is_conservative():
    out = promotion_decision({
        "forward_days":30,"closed_trades":5,"total_return":.10,
        "sharpe":1.0,"max_drawdown":-.10
    })
    assert out["eligible_for_extended_paper"] is False
    assert "forward_days<60" in out["reasons"]


def test_rank_forward_handles_zero_forward_bars(tmp_path):
    db = ForwardDB(tmp_path/"f.sqlite3")
    reg = pd.Timestamp("2026-08-20T00:00:00Z").isoformat()
    register(db, reg)
    out = rank_forward(db)
    assert len(out) == 1
    assert out.iloc[0]["forward_evidence"] == 0.0
    assert out.iloc[0]["forward_score"] == 0.0
    assert out.iloc[0]["forward_bars"] == 0
