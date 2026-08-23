from __future__ import annotations

from datetime import datetime, timezone
import os
import re

import pandas as pd
import yaml

from .dynamic_universe import DynamicUniverse
from .forward_db import ForwardDB
from .paths import db_path
from .twstock_support import (
    TW_MARKET,
    TaiwanMarketCache,
    TaiwanSimulationDB,
    TaiwanSimulationLab,
    normalize_tw_symbol,
)

RECALIBRATE_HOURS = {"short": 72, "medium": 168, "long": 336}
_HORIZONS = ("short", "medium", "long")


def _waiting_history_error(exc: Exception):
    text = str(exc)
    m = re.search(r"Need at least\s+(\d+)\s+closed bars", text, flags=re.I)
    return int(m.group(1)) if m else None


def _calibration_budget():
    try:
        return max(1, int(os.getenv("V6_CALIBRATIONS_PER_CYCLE", "4")))
    except Exception:
        return 4


class AutoOrchestratorV8:
    """Research + local virtual broker only. Broker order API remains disabled."""

    def __init__(self, initial_equity=100000.0):
        self.forward = ForwardDB(db_path("forward_validation.sqlite3"))
        self.db = TaiwanSimulationDB(db_path("simulation_lab.sqlite3"))
        self.cache = TaiwanMarketCache(db_path("market_cache.sqlite3"))
        self.lab = TaiwanSimulationLab(self.db, self.cache, initial_equity=float(initial_equity))
        self.universe = DynamicUniverse(self.db)
        self._bootstrap_twstocks()

    def _configured_twstocks(self):
        enabled = os.getenv("V6_ENABLE_TWSTOCKS", "1").strip().lower() not in ("0", "false", "no", "off")
        if not enabled:
            return []
        env_symbols = os.getenv("V6_TWSTOCK_SYMBOLS", "").strip()
        if env_symbols:
            return [normalize_tw_symbol(x.strip()) for x in env_symbols.split(",") if x.strip()]
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return [normalize_tw_symbol(x) for x in ((cfg.get("universe") or {}).get("twstocks") or [])]
        except Exception:
            return []

    def _bootstrap_twstocks(self):
        rows = [{"market": TW_MARKET, "symbol": s} for s in self._configured_twstocks()]
        return self.lab.import_assets(rows)

    def import_active(self):
        return self.lab.import_assets(self.forward.candidates("ACTIVE"))

    def _pinned_universe(self):
        pinned = {"stock": set(), "crypto": set(), TW_MARKET: set(self._configured_twstocks())}
        for r in self.forward.candidates("ACTIVE"):
            market = str(r.get("market") or "")
            symbol = str(r.get("symbol") or "").upper()
            if market in pinned and symbol:
                pinned[market].add(symbol)
        return {k: sorted(v) for k, v in pinned.items()}

    def _model_due(self, market, symbol, horizon, now):
        m = self.db.model(market, symbol, horizon)
        if m is None:
            return True
        try:
            t = pd.Timestamp(m.get("updated_at"))
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            return (now - t).total_seconds() >= RECALIBRATE_HOURS[horizon] * 3600
        except Exception:
            return True

    def model_health(self):
        assets = self.db.assets()
        total = len(assets) * len(_HORIZONS)
        ready = 0
        for a in assets:
            for hz in _HORIZONS:
                if self.db.model(a["market"], a["symbol"], hz) is not None:
                    ready += 1
        return {"total_pairs": total, "ready_pairs": ready, "unready_pairs": max(0, total - ready)}

    def calibrate_due(self, now=None, force=False):
        now = pd.Timestamp(now or datetime.now(timezone.utc))
        now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        done, errors, waiting = [], [], []
        attempts = 0
        budget = None if force else _calibration_budget()
        for a in self.db.assets():
            for hz in _HORIZONS:
                if not force and not self._model_due(a["market"], a["symbol"], hz, now):
                    continue
                if budget is not None and attempts >= budget:
                    return {"calibrated": len(done), "waiting_history": waiting, "errors": errors,
                            "budget": budget, "budget_exhausted": True}
                attempts += 1
                try:
                    done.append(self.lab.calibrate(a["market"], a["symbol"], hz, now))
                except Exception as e:
                    required = _waiting_history_error(e)
                    row = {"market": a["market"], "symbol": a["symbol"], "horizon": hz}
                    if required is not None:
                        waiting.append({**row, "required_closed_bars": required, "status": "WAITING_FOR_HISTORY"})
                    else:
                        errors.append({**row, "error": f"{type(e).__name__}: {e}"})
        return {"calibrated": len(done), "waiting_history": waiting, "errors": errors,
                "budget": budget, "budget_exhausted": False}

    def _run_ready_once(self, now=None):
        checked = processed = fetched = api_calls = 0
        errors = []
        skipped_unready = 0
        for a in self.db.assets():
            for hz in _HORIZONS:
                if self.db.model(a["market"], a["symbol"], hz) is None:
                    skipped_unready += 1
                    continue
                checked += 1
                try:
                    r = self.lab.process_asset_horizon(a["market"], a["symbol"], hz, now)
                    processed += int(r.get("processed", 0))
                    fetched += int(r.get("fetched", 0))
                    api_calls += int(bool(r.get("api_called", False)))
                except Exception as e:
                    errors.append({"market": a["market"], "symbol": a["symbol"], "horizon": hz,
                                   "error": f"{type(e).__name__}: {e}"})
        return {
            "status": "OK" if not errors else "PARTIAL",
            "assets_checked": checked,
            "bars_processed": processed,
            "market_data_api_calls": api_calls,
            "api_rows_fetched": fetched,
            "broker_order_api_calls": 0,
            "skipped_unready_pairs": skipped_unready,
            "errors": errors,
        }

    def full_cycle(self, now=None, force_recalibrate=False):
        imported = self.import_active()
        self._bootstrap_twstocks()
        universe = self.universe.refresh_due(self._pinned_universe(), force=False)
        cal = self.calibrate_due(now, force_recalibrate)
        run = self._run_ready_once(now)
        run_errors = run.get("errors") or []
        true_errors = [*cal.get("errors", []), *run_errors]
        health = self.model_health()
        waiting = cal.get("waiting_history", [])
        return {
            "status": "OK" if not true_errors else "PARTIAL",
            "imported": imported,
            "universe": universe,
            "calibration": cal,
            "simulation": run,
            "health": {**health, "waiting_history": len(waiting), "true_errors": len(true_errors)},
            "true_errors": true_errors,
            "broker_order_api_calls": 0,
        }
