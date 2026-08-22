import pandas as pd
import numpy as np
from src.decision_engine import decision_for
from src.live_analytics import problem_ranking
from src.simulation_db import SimulationDB


def fake_df(n=400):
    idx=pd.date_range('2025-01-01',periods=n,freq='h',tz='UTC')
    close=pd.Series(np.linspace(100,150,n)+np.sin(np.arange(n)/8),index=idx)
    return pd.DataFrame({'open':close*0.999,'high':close*1.01,'low':close*0.99,'close':close,'volume':1000.0},index=idx)


def test_decision_has_split_confidence():
    m={'strategy':'Trend MA','params':{'fast':8,'slow':30},'oos_score':80,'train_score':78,'calibration_score':79,'diagnostics':{'stability':85,'sample':1.0}}
    d=decision_for(fake_df(),'crypto','short',m,100000)
    assert 0 <= d['confidence'] <= 100
    assert 'model_confidence' in d['diagnostics']
    assert 'signal_strength' in d['diagnostics']
    assert 'regime_score' in d['diagnostics']


def test_problem_ranking_empty(tmp_path):
    db=SimulationDB(str(tmp_path/'s.sqlite3')); db.ensure_accounts(100000)
    assert problem_ranking(db).empty
