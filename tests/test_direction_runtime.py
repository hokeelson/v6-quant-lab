from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import direction_shadow_worker as worker
import direction_shadow_supervisor as supervisor
import runtime_health_exporter as health
import storage_rescue as rescue
from src.direction_forward import DirectionForwardLedger
from src.market_cache import MarketCache
from src.simulation_db import SimulationDB


def stamp(age=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()


class DirectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.env = patch.dict(os.environ, {"V6_DATA_DIR": str(self.runtime)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def seed(self, models=True, data=True):
        db = SimulationDB(str(self.runtime / "simulation_lab.sqlite3"))
        cache = MarketCache(str(self.runtime / "market_cache.sqlite3"))
        db.add_asset("crypto", "TESTUSDT")
        if models:
            db.save_model({
                "market": "crypto", "symbol": "TESTUSDT", "horizon": "short",
                "strategy": "Trend MA", "params": {"fast": 20, "slow": 60},
                "calibration_score": 70, "oos_score": 65, "train_score": 70,
                "regime_fit": 1, "calibrated_through": stamp(), "updated_at": stamp(),
                "diagnostics": {"stability": 80, "sample": 1},
            })
        end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=8)
        index = pd.date_range(end=end, periods=160, freq="h")
        close = 100 * np.exp(np.arange(len(index)) * 0.002)
        frame = pd.DataFrame({"open": close, "high": close * 1.004,
                              "low": close * 0.996, "close": close, "volume": 1000.0}, index=index)
        if data:
            cache.upsert("crypto", "TESTUSDT", "1h", frame)
        return db, cache, frame

    def test_shared_runtime_paths_override_empty_app_database(self):
        db, cache, frame = self.seed()
        app = self.root / "app"
        app.mkdir()
        SimulationDB(str(app / "simulation_lab.sqlite3"))
        with patch("src.direction_engine.external_intelligence_assessment", return_value={}), \
             patch.object(MarketCache, "ensure", side_effect=AssertionError("No API allowed")):
            old_cwd = Path.cwd()
            try:
                os.chdir(app)
                runtime_db, runtime_cache, ledger = worker.open_runtime()
                self.assertEqual(Path(runtime_db.path), self.runtime / "simulation_lab.sqlite3")
                self.assertEqual(Path(runtime_cache.path), self.runtime / "market_cache.sqlite3")
                result = worker.build_snapshot(runtime_db, runtime_cache, ledger)
                self.assertEqual(result["status"], "ONLINE")
                self.assertEqual(result["forward"]["pending"], 1)
                self.assertEqual(result["summary"]["registered"], 1)
                self.assertEqual(result["market_data_api_calls"], 0)
                self.assertEqual(result["broker_order_api_calls"], 0)
                again = worker.build_snapshot(runtime_db, runtime_cache, ledger)
                self.assertEqual(again["forward"]["pending"], 1)
                self.assertEqual(again["summary"]["registered"], 0)
            finally:
                os.chdir(old_cwd)
        self.assertEqual(db.assets()[0]["symbol"], "TESTUSDT")

    def test_missing_shared_inputs_fail_without_creating_empty_database(self):
        with self.assertRaises(FileNotFoundError):
            worker.open_runtime()
        self.assertFalse((self.runtime / "simulation_lab.sqlite3").exists())
        self.assertFalse((self.runtime / "market_cache.sqlite3").exists())

    def test_model_and_cache_skips_are_visible_not_healthy(self):
        db, cache, _ = self.seed(models=False)
        ledger = DirectionForwardLedger(str(self.runtime / "direction_forward.sqlite3"))
        result = worker.build_snapshot(db, cache, ledger)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["summary"]["missing_models"], 3)
        self.assertEqual(result["summary"]["candidates"], 0)

    def test_missing_cached_data_does_not_fetch_or_report_healthy(self):
        db, cache, _ = self.seed(data=False)
        ledger = DirectionForwardLedger(str(self.runtime / "direction_forward.sqlite3"))
        with patch.object(MarketCache, "ensure", side_effect=AssertionError("No API allowed")):
            result = worker.build_snapshot(db, cache, ledger)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["summary"]["insufficient_cache"], 1)

    def test_fatal_cycle_error_is_persisted(self):
        status = worker.WorkerStatus(self.runtime / "direction_status.json")
        with patch.object(worker, "open_runtime", side_effect=RuntimeError("test")):
            self.assertIsNone(worker.run_cycle(status))
        result = json.loads(status.path.read_text())
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["true_errors"], 1)
        self.assertIn("heartbeat_at", result)

    def test_cycle_writes_counts_to_heartbeat(self):
        self.seed()
        status = worker.WorkerStatus(self.runtime / "direction_status.json")
        with patch.object(worker, "PUBLIC_PATH", self.root / "public.json"), \
             patch("src.direction_engine.external_intelligence_assessment", return_value={}):
            result = worker.run_cycle(status)
        saved = json.loads(status.path.read_text())
        self.assertEqual(result["forward"]["pending"], 1)
        self.assertEqual(saved["pending"], 1)
        self.assertEqual(saved["status"], "ONLINE")
        self.assertEqual(saved["input_path_mode"], "SHARED_V6_DATA_DIR")

    def test_synthetic_closed_bars_evaluate_after_registration(self):
        db, cache, frame = self.seed()
        ledger = DirectionForwardLedger(str(self.runtime / "direction_forward.sqlite3"))
        with patch("src.direction_engine.external_intelligence_assessment", return_value={}):
            worker.build_snapshot(db, cache, ledger)
            idx = pd.date_range(frame.index[-1] + pd.Timedelta(hours=1), periods=6, freq="h")
            price = frame.close.iloc[-1] * np.exp(np.arange(1, 7) * 0.002)
            future = pd.DataFrame({"open": price, "high": price * 1.004, "low": price * 0.996,
                                   "close": price, "volume": 1000.0}, index=idx)
            cache.upsert("crypto", "TESTUSDT", "1h", future)
            result = worker.build_snapshot(db, cache, ledger)
        self.assertEqual(result["forward"]["evaluated"], 1)
        self.assertEqual(result["forward"]["pending"], 1)

    def test_direction_snapshot_restore_preserves_registered_evidence(self):
        db, cache, _ = self.seed()
        ledger = DirectionForwardLedger(str(self.runtime / "direction_forward.sqlite3"))
        with patch("src.direction_engine.external_intelligence_assessment", return_value={}):
            worker.build_snapshot(db, cache, ledger)
        original_key = ledger.recent(1)[0]["prediction_key"]
        persist = self.root / "persistent"
        current = persist / "v6-snapshots" / "current"
        archive = persist / "v6-snapshots" / "archive"
        with patch.multiple(rescue, PERSIST_DIR=persist, CURRENT_DIR=current, ARCHIVE_DIR=archive,
                            SNAPSHOT_DIR=persist / "v6-snapshots",
                            RUNTIME_DIR=self.runtime, STATUS_PATH=self.runtime / "storage.json"):
            rescue.persist_one(Path(ledger.path), "synthetic")
            backup = json.loads((self.runtime / "direction_forward_backup_status.json").read_text())
            self.assertTrue(backup["success"])
            self.assertEqual(backup["pending"], 1)
            new_runtime = self.root / "after-restart"
            with patch.multiple(rescue, RUNTIME_DIR=new_runtime, STATUS_PATH=new_runtime / "storage.json"):
                rescue.bootstrap_runtime()
                restored = DirectionForwardLedger(str(new_runtime / "direction_forward.sqlite3"))
                self.assertEqual(restored.summary()["pending"], 1)
                self.assertEqual(restored.recent(1)[0]["prediction_key"], original_key)

    def test_backup_is_mandatory_and_started_under_supervisor(self):
        self.assertIn("direction_forward.sqlite3", rescue.CRITICAL_DBS)
        cloud = (Path(worker.__file__).parent / "cloud_start.sh").read_text()
        self.assertIn("direction_forward.sqlite3", cloud)
        self.assertIn("python direction_shadow_supervisor.py &", cloud)

    def test_direction_health_checks_counts_freshness_and_backup(self):
        status = {"status": "ONLINE", "heartbeat_at": stamp(5), "last_cycle_finished_at": stamp(900),
                  "candidates": 1, "pending": 1, "evaluated": 0, "shared_cache_only": True}
        backup = {"success": True, "last_snapshot_at": stamp(30), "pending": 1, "evaluated": 0}
        self.assertTrue(health._direction_health(status, backup)["healthy"])
        self.assertFalse(health._direction_health({}, backup)["healthy"])
        self.assertFalse(health._direction_health({**status, "pending": 0}, backup)["healthy"])
        self.assertFalse(health._direction_health({**status, "heartbeat_at": stamp(120)}, backup)["healthy"])
        self.assertFalse(health._direction_health({**status, "true_errors": 1}, backup)["healthy"])
        self.assertFalse(health._direction_health(status, {})["healthy"])
        self.assertFalse(health._direction_health(status, {**backup, "pending": 0})["healthy"])

    def test_supervisor_allows_normal_idle_and_restarts_stale_or_stalled_worker(self):
        status = {"pid": 123, "status": "ONLINE", "heartbeat_at": stamp(5),
                  "last_cycle_finished_at": stamp(950)}
        self.assertIsNone(supervisor.restart_reason(status, 123, 1000))
        self.assertIsNone(supervisor.restart_reason({}, 123, 20))
        self.assertEqual(supervisor.restart_reason({}, 123, 1000), "missing_current_worker_status")
        self.assertEqual(supervisor.restart_reason({**status, "heartbeat_at": stamp(120)}, 123, 1000),
                         "stale_heartbeat")
        self.assertEqual(supervisor.restart_reason({**status, "status": "RUNNING",
                         "last_cycle_started_at": stamp(1900)}, 123, 2000), "stalled_cycle")


    def test_direction_backup_can_run_without_legacy_snapshot_loop(self):
        self.seed()
        ledger = DirectionForwardLedger(str(self.runtime / "direction_forward.sqlite3"))
        with patch("src.direction_engine.external_intelligence_assessment", return_value={}):
            db, cache, _ = worker.open_runtime()
            worker.build_snapshot(db, cache, ledger)
        persist = self.root / "persistent"
        snapshot_dir = persist / "v6-snapshots"
        with patch.multiple(rescue, PERSIST_DIR=persist, RUNTIME_DIR=self.runtime,
                            SNAPSHOT_DIR=snapshot_dir, CURRENT_DIR=snapshot_dir / "current",
                            ARCHIVE_DIR=snapshot_dir / "archive"), \
             patch.object(rescue, "snapshot_all", side_effect=AssertionError("legacy loop blocked")):
            self.assertTrue(rescue.snapshot_direction())
            status = json.loads((self.runtime / "direction_forward_backup_status.json").read_text())
            self.assertEqual(status["pending"], 1)


if __name__ == "__main__":
    unittest.main()
