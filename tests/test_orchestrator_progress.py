from __future__ import annotations

from types import SimpleNamespace

from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.champion_challenger import ChampionChallenger


def test_simulation_publishes_counts_before_the_whole_cycle_finishes():
    events = []
    engine = AutoOrchestratorV8.__new__(AutoOrchestratorV8)
    engine.db = SimpleNamespace(
        assets=lambda: [{"market": "crypto", "symbol": "BTCUSDT"}],
        model=lambda market, symbol, horizon: None if horizon == "long" else {"strategy": "test"},
    )
    def process(market, symbol, horizon, now):
        assert events[-1][1]["unit"] == f"{market}:{symbol}:{horizon}"
        if horizon == "medium":
            raise RuntimeError("fixture failure")
        return {"processed": 2, "fetched": 3, "api_called": True}
    engine.lab = SimpleNamespace(process_asset_horizon=process)
    result = engine._run_ready_once(progress=lambda phase, **kw: events.append((phase, kw)))
    assert result["assets_checked"] == 2
    assert result["bars_processed"] == 2
    assert result["market_data_api_calls"] == 1
    assert result["skipped_unready_pairs"] == 1
    assert len(result["errors"]) == 1
    assert result["broker_order_api_calls"] == 0
    assert events[-1][1]["completed"] == events[-1][1]["total"] == 3
    metric_events = [kw["metrics"] for _, kw in events if "metrics" in kw]
    assert metric_events[0] == {"assets_checked": 1, "bars_processed": 2, "market_data_api_calls": 1}
    assert metric_events[-1]["assets_checked"] == 2


def test_calibration_reports_completed_attempts_even_on_error(monkeypatch):
    monkeypatch.setenv("V6_CALIBRATIONS_PER_CYCLE", "1")
    events = []
    engine = AutoOrchestratorV8.__new__(AutoOrchestratorV8)
    engine.db = SimpleNamespace(
        assets=lambda: [{"market": "crypto", "symbol": "BTCUSDT"}],
        model=lambda *args: None,
    )
    engine.governance = SimpleNamespace(active_arena=lambda *args: None)
    engine._model_due = lambda *args: True
    def fail(*args):
        raise RuntimeError("fixture failure")
    engine.lab = SimpleNamespace(calibrate=fail)
    result = engine.calibrate_due(progress=lambda phase, **kw: events.append((phase, kw)))
    assert result["budget_exhausted"] is True
    assert len(result["errors"]) == 1
    assert [kw["completed"] for _, kw in events] == [0, 1]
    assert all(phase == "CALIBRATION" for phase, _ in events)


def test_full_cycle_preserves_business_order_and_reports_each_phase():
    calls, phases = [], []
    engine = AutoOrchestratorV8.__new__(AutoOrchestratorV8)
    engine.db, engine.cache = object(), object()
    def result(name, data):
        def call(*args, **kwargs):
            calls.append(name)
            return data
        return call
    engine.import_active = result("import", 0)
    engine._bootstrap_twstocks = result("bootstrap", 0)
    engine._pinned_universe = lambda: {}
    engine.universe = SimpleNamespace(refresh_due=result("universe", {}))
    engine.calibrate_due = result("calibration", {"errors": [], "waiting_history": []})
    engine._run_ready_once = result("simulation", {"errors": [], "broker_order_api_calls": 0})
    engine.governance = SimpleNamespace(process_active=result("governance", {"errors": []}))
    engine.model_health = result("health", {})
    engine.forward_health = result("forward_health", {})
    output = engine.full_cycle(progress=lambda phase, **kw: phases.append(phase))
    assert calls == ["import", "bootstrap", "universe", "calibration", "simulation", "governance", "health", "forward_health"]
    assert phases == ["PREPARE", "UNIVERSE", "CALIBRATION", "SIMULATION", "GOVERNANCE", "MODEL_HEALTH"]
    assert output["status"] == "OK"
    assert output["broker_order_api_calls"] == 0


def test_governance_reports_completion_when_waiting_for_data():
    events = []
    governance = ChampionChallenger.__new__(ChampionChallenger)
    governance.arenas = lambda status: [{"market": "crypto", "symbol": "BTCUSDT", "horizon": "short"}]
    cache = SimpleNamespace(ensure=lambda *args: {"data": None}, closed_only=lambda *args: None)
    result = governance.process_active(object(), cache, progress=lambda phase, **kw: events.append((phase, kw)))
    assert result["arenas_checked"] == 1
    assert result["errors"] == []
    assert [kw["completed"] for _, kw in events] == [0, 1]
