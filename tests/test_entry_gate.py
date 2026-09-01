import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src import risk_sizing as rs
from src.entry_gate import ENTRY_POLICY_VERSION, finalize_entry, multiplier, safe_entry_sizing
from src.entry_gate_audit import entry_gate_audit
from src.expected_live_sizing import blend_expected_live_multiplier
from src.twstock_support import TaiwanSimulationDB, TaiwanSimulationLab


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.pre = {"candidates": [{"market": "crypto", "symbol": "TEST", "horizon": "short",
                                    "shadow_size_multiplier": 1.0, "verdict": "ALLOW"}]}
        self.ev = SimpleNamespace(probability_win=.6, expected_value_pct=.02,
                                  expected_value_r=.5, evidence_trades=20, evidence_weight=1.0)
        self.mocks = {
            "build_pretrade_risk_snapshot": lambda *a: self.pre,
            "portfolio_risk_snapshot": lambda *a: {"groups": []},
            "estimate_trade_ev": lambda **k: self.ev,
            "score_portfolio_candidate": lambda *a: SimpleNamespace(portfolio_ev_score=.5),
            "strategy_health_snapshot": lambda *a: {"strategies": [], "regimes": []},
            "symbol_strategy_health_snapshot": lambda *a: {"symbols": []},
            "expected_live_sizing_assessment": lambda *a: {},
            "meta_entry_assessment": lambda *a: {"meta_multiplier": 1.0, "meta_verdict": "ALLOW"},
            "assess_pair": lambda *a: {"quality_drift_multiplier": 1.0, "data_status": "OK"},
        }
        p = patch.multiple(rs, **self.mocks)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(rs, "_flag", side_effect=lambda *a: True)
        p.start()
        self.addCleanup(p.stop)
        self.db = SimpleNamespace(model=lambda *a: {}, account=lambda *a: {"cash": 100000},
                                  marks=lambda *a: {}, positions=lambda *a: [])

    def run_sizing(self, amount=1000):
        return rs.active_entry_sizing(self.db, None, "crypto", "TEST", "short",
                                     {"strategy": "Momentum", "regime": "NORMAL_UP_TREND"}, amount)

    def test_healthy_entry_remains_allowed(self):
        result = self.run_sizing()
        self.assertTrue(result["entry_allowed"])
        self.assertEqual(result["adjusted_notional"], 1000)

    def test_zero_and_small_sizes_are_not_raised(self):
        for value in (0, .1):
            with self.subTest(value=value):
                self.pre["candidates"][0]["shadow_size_multiplier"] = value
                result = self.run_sizing()
                self.assertEqual(result["adjusted_notional"], value * 1000)
                self.assertEqual(result["entry_allowed"], value > 0)

    def test_invalid_multipliers_fail_closed(self):
        for value in (float("nan"), float("inf"), -.1, 1.1, "bad"):
            with self.subTest(value=value):
                self.pre["candidates"][0]["shadow_size_multiplier"] = value
                self.assertEqual(self.run_sizing()["adjusted_notional"], 0)

    def test_all_layer_exceptions_fail_closed(self):
        for name in self.mocks:
            with self.subTest(layer=name), patch.object(rs, name, side_effect=RuntimeError("test")):
                result = self.run_sizing()
                self.assertFalse(result["entry_allowed"])
                self.assertEqual(result["adjusted_notional"], 0)
        with patch.object(rs, "cost_aware_leverage_room", side_effect=RuntimeError("test")):
            self.assertFalse(self.run_sizing()["entry_allowed"])

    def test_block_verdicts_override_positive_size(self):
        cases = [
            ("pretrade_verdict", "BLOCK_CANDIDATE"), ("meta_verdict", "SHADOW_ONLY"),
            ("strategy_state", "PAUSE_CANDIDATE"), ("regime_state", "SHADOW_ONLY_CANDIDATE"),
            ("symbol_strategy_state", "PAUSE_CANDIDATE"), ("expected_live_state", "QUARANTINED"),
        ]
        for key, value in cases:
            with self.subTest(key=key):
                r = finalize_entry({"original_notional": 1000, "adjusted_notional": 250, key: value})
                self.assertEqual(r["adjusted_notional"], 0)
                self.assertIn(key + ":" + value, r["entry_block_reasons"])
        self.pre["candidates"][0]["verdict"] = "BLOCK_CANDIDATE"
        self.assertFalse(self.run_sizing()["entry_allowed"])

    def test_real_meta_and_quarantine_paths(self):
        with patch.object(rs, "meta_entry_assessment", return_value={
                "meta_verdict": "SHADOW_ONLY", "meta_multiplier": .6}):
            self.assertFalse(self.run_sizing()["entry_allowed"])
        with patch.object(rs, "expected_live_sizing_assessment", return_value={
                "expected_live_state": "QUARANTINED", "expected_live_multiplier": .25}):
            self.assertFalse(self.run_sizing()["entry_allowed"])

    def test_negative_ev_maturity(self):
        self.ev.expected_value_pct = -.01
        self.ev.expected_value_r = -.2
        self.assertIn("MATURE_NEGATIVE_EV", self.run_sizing()["entry_block_reasons"])
        self.ev.evidence_weight = .1
        self.assertTrue(self.run_sizing()["entry_allowed"])

    def test_bad_requests_and_assessor_contract(self):
        for value in (-1, float("nan"), float("inf"), "bad", None):
            with self.subTest(value=value):
                self.assertFalse(self.run_sizing(value)["entry_allowed"])
        for assessor in (lambda: None, lambda: {"adjusted_notional": 1000},
                         lambda: 1 / 0):
            self.assertFalse(safe_entry_sizing(assessor)["entry_allowed"])

    def test_zero_blending_and_multiplier(self):
        self.assertEqual(multiplier(0), 0)
        self.assertEqual(multiplier(None), 1)
        self.assertAlmostEqual(blend_expected_live_multiplier(0, 30)[0], .1)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = TaiwanSimulationDB(str(Path(self.temp.name) / "sim.sqlite3"))
        self.lab = TaiwanSimulationLab(self.db, SimpleNamespace())
        self.ts = pd.Timestamp("2026-09-01T00:00:00Z")
        self.row = pd.Series({"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0})

    def order(self, market, side="BUY"):
        aid = market + "_short"
        oid = self.db.add_order({"account_id": aid, "symbol": "TEST", "side": side,
                                "created_bar": "2026-08-31T00:00:00Z", "requested_notional": 1000,
                                "qty": 10 if side == "SELL" else None, "reason": "TEST",
                                "decision_id": None})
        return aid, oid

    def position(self, aid):
        return {"account_id": aid, "symbol": "TEST", "qty": 10, "avg_entry": 100,
                "entry_bar": "2026-08-31T00:00:00Z", "strategy": "Momentum", "horizon": "short",
                "regime_entry": "UP", "stop_price": 90, "target_price": 130,
                "max_holding_bars": 100, "bars_held": 0, "leverage_at_entry": 1}

    def test_blocked_buy_never_spends_and_is_idempotent_all_markets(self):
        for market in ("stock", "crypto", "twstock"):
            aid, oid = self.order(market)
            target = "src.twstock_support" if market == "twstock" else "src.simulation_engine"
            blocked = finalize_entry({"original_notional": 1000, "adjusted_notional": 250,
                                      "expected_live_state": "QUARANTINED"})
            with patch(target + ".active_entry_sizing", return_value=blocked):
                self.assertEqual(self.lab._execute_pending(aid, market, "TEST", self.ts, self.row), "CANCELLED")
                self.assertIsNone(self.lab._execute_pending(aid, market, "TEST", self.ts, self.row))
            self.assertEqual(self.db.account(aid)["cash"], 100000)
            self.assertIsNone(self.db.position(aid, "TEST"))
            with self.db._c() as con:
                self.assertEqual(con.execute("SELECT reason FROM orders WHERE order_id=?", (oid,)).fetchone()[0],
                                 "ENTRY_GATE_BLOCKED")
        report = entry_gate_audit(self.db.path)
        self.assertEqual(report["summary"]["blocked"], 3)
        self.assertEqual(report["summary"]["blocked_but_filled"], 0)

    def test_unexpected_assessor_failure_cancels_buy(self):
        aid, _ = self.order("stock")
        with patch("src.simulation_engine.active_entry_sizing", side_effect=RuntimeError("test")):
            self.assertEqual(self.lab._execute_pending(aid, "stock", "TEST", self.ts, self.row), "CANCELLED")
        self.assertEqual(self.db.account(aid)["cash"], 100000)

    def test_healthy_buy_can_fill_all_markets(self):
        for market in ("stock", "crypto", "twstock"):
            aid, _ = self.order(market)
            target = "src.twstock_support" if market == "twstock" else "src.simulation_engine"
            allowed = finalize_entry({"original_notional": 1000, "adjusted_notional": 1000})
            with patch(target + ".active_entry_sizing", return_value=allowed):
                self.assertEqual(self.lab._execute_pending(aid, market, "TEST", self.ts, self.row), "BUY")
            self.assertIsNotNone(self.db.position(aid, "TEST"))
        self.assertEqual(entry_gate_audit(self.db.path)["summary"]["filled"], 3)

    def test_sell_bypasses_admission_all_markets(self):
        for market in ("stock", "crypto", "twstock"):
            aid, _ = self.order(market, "SELL")
            self.db.upsert_position(self.position(aid))
            target = "src.twstock_support" if market == "twstock" else "src.simulation_engine"
            with patch(target + ".active_entry_sizing", side_effect=AssertionError("must not run")):
                self.assertEqual(self.lab._execute_pending(aid, market, "TEST", self.ts, self.row), "SELL")
            self.assertIsNone(self.db.position(aid, "TEST"))
        self.assertEqual(len(self.db.recent_trades()), 3)

    def test_protective_exit_still_works(self):
        aid = "stock_short"
        self.db.upsert_position(self.position(aid))
        bar = pd.Series({"open": 80, "high": 85, "low": 75, "close": 82})
        with patch("src.simulation_engine.active_entry_sizing", side_effect=AssertionError("must not run")):
            self.assertEqual(self.lab._protect_position(aid, "stock", "TEST", self.ts, bar), "ATR_STOP_GAP")
        self.assertIsNone(self.db.position(aid, "TEST"))

    def test_audit_detects_forbidden_fill_and_excludes_legacy(self):
        self.db.add_diagnostic("stock_short", "TEST", "short", str(self.ts), "RISK_SIZING", "legacy",
                               {"filled_notional": 1000})
        self.assertEqual(entry_gate_audit(self.db.path)["summary"]["sampled_events"], 0)
        self.db.add_diagnostic("stock_short", "TEST", "short", str(self.ts), "RISK_SIZING", "test",
                               {"entry_policy_version": ENTRY_POLICY_VERSION,
                                "entry_allowed": False, "filled_notional": 1000})
        self.assertEqual(entry_gate_audit(self.db.path)["summary"]["blocked_but_filled"], 1)
        self.assertEqual(entry_gate_audit(self.db.path)["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
