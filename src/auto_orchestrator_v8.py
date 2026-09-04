from __future__ import annotations

from datetime import datetime, timezone
import os
import re
import time

import pandas as pd
import yaml

from .champion_challenger import ChampionChallenger, model_signature
from .decision_engine import calibrate_asset
from .dynamic_universe import DynamicUniverse
from .forward_db import ForwardDB
from .paths import db_path
from .worker_progress import notify_progress
from .twstock_support import (
    TW_MARKET,
    TaiwanMarketCache,
    TaiwanSimulationDB,
    TaiwanSimulationLab,
    calibrate_twstock,
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
    """Research + virtual broker only. Broker order API remains disabled."""

    def __init__(self, initial_equity=100000.0):
        self.initial_equity = float(initial_equity)

        # Forward validation is useful research state, but it must never become a
        # single point of failure for the live dashboard/simulation. Railway volume
        # hand-offs can occasionally leave one SQLite file temporarily unreadable
        # with `disk I/O error`. Preserve the primary file untouched and fail over
        # to an ephemeral isolated DB so the rest of V6 can stay online.
        self.forward_primary_path = db_path("forward_validation.sqlite3")
        self.forward_degraded = False
        self.forward_error = None
        self.forward_fallback_path = os.getenv(
            "V6_FORWARD_FALLBACK_PATH", "/tmp/v6_forward_validation_fallback.sqlite3"
        )
        try:
            self.forward = ForwardDB(self.forward_primary_path)
        except Exception as exc:
            self.forward_degraded = True
            self.forward_error = f"{type(exc).__name__}: {exc}"
            self.forward = ForwardDB(self.forward_fallback_path)

        self.db = TaiwanSimulationDB(db_path("simulation_lab.sqlite3"))
        self.cache = TaiwanMarketCache(db_path("market_cache.sqlite3"))
        self.lab = TaiwanSimulationLab(self.db, self.cache, initial_equity=self.initial_equity)
        self.universe = DynamicUniverse(self.db)
        self.governance = ChampionChallenger(db_path("model_governance.sqlite3"), self.initial_equity)
        # Crypto Lite: do not auto-bootstrap Taiwan stocks.

    def forward_health(self):
        return {
            "status": "DEGRADED" if self.forward_degraded else "ONLINE",
            "primary_path": self.forward_primary_path,
            "active_path": self.forward_fallback_path if self.forward_degraded else self.forward_primary_path,
            "error": self.forward_error,
            "fallback_ephemeral": bool(self.forward_degraded),
        }

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
        # Crypto Lite: only import active crypto candidates.
        rows = [r for r in self.forward.candidates("ACTIVE") if str(r.get("market") or "") == "crypto"]
        return self.lab.import_assets(rows)

    def _pinned_universe(self):
        pinned = {"stock": set(), "crypto": set(), TW_MARKET: set()}
        for r in self.forward.candidates("ACTIVE"):
            market = str(r.get("market") or "")
            symbol = str(r.get("symbol") or "").upper()
            if market == "crypto" and symbol:
                pinned["crypto"].add(symbol)
        # An active Champion/Challenger arena must not lose its symbol from the
        # dynamic universe before the paired forward comparison is decided.
        for r in self.governance.arenas("ACTIVE"):
            market = str(r.get("market") or "")
            symbol = str(r.get("symbol") or "").upper()
            if market == "crypto" and symbol:
                pinned["crypto"].add(symbol)
        return {k: sorted(v) for k, v in pinned.items()}

    def _model_due(self, market, symbol, horizon, now):
        m = self.db.model(market, symbol, horizon)
        if m is None:
            return True
        if self.governance.active_arena(market, symbol, horizon):
            return False
        raw = self.governance.last_research_at(market, symbol, horizon) or m.get("updated_at")
        try:
            t = pd.Timestamp(raw)
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            return (now - t).total_seconds() >= RECALIBRATE_HOURS[horizon] * 3600
        except Exception:
            return True

    def _candidate_model(self, market, symbol, horizon, now):
        pack = self.cache.ensure(market, symbol, horizon, now)
        df = self.cache.closed_only(pack["data"], market, horizon, now)
        if market == TW_MARKET:
            model = calibrate_twstock(df, horizon, self.initial_equity)
            symbol = normalize_tw_symbol(symbol)
        else:
            model = calibrate_asset(df, market, horizon, self.initial_equity)
        now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
        now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")
        model.update({
            "market": market,
            "symbol": str(symbol).upper(),
            "horizon": horizon,
            "updated_at": now_ts.isoformat(),
        })
        return model, pack

    def model_health(self):
        assets = [a for a in self.db.assets() if a.get("market") == "crypto"]
        total = len(assets) * len(_HORIZONS)
        ready = 0
        for a in assets:
            for hz in _HORIZONS:
                if self.db.model(a["market"], a["symbol"], hz) is not None:
                    ready += 1
        return {"total_pairs": total, "ready_pairs": ready, "unready_pairs": max(0, total - ready)}

    def calibrate_due(self, now=None, force=False, progress=None):
        now = pd.Timestamp(now or datetime.now(timezone.utc))
        now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        done, errors, waiting = [], [], []
        attempts = 0
        # Forced calibration still respects an active arena: repeatedly generating
        # challengers while one is already in forward validation would data-mine
        # the same future period and defeat the governance layer.
        budget = _calibration_budget()
        for a in [x for x in self.db.assets() if x.get("market") == "crypto"]:
            for hz in _HORIZONS:
                market, symbol = a["market"], a["symbol"]
                if self.governance.active_arena(market, symbol, hz):
                    continue
                if not force and not self._model_due(market, symbol, hz, now):
                    continue
                if attempts >= budget:
                    return {"calibrated": len(done), "waiting_history": waiting, "errors": errors,
                            "budget": budget, "budget_exhausted": True, "forced": bool(force)}
                attempts += 1
                unit = f"{market}:{symbol}:{hz}"
                notify_progress(progress, "CALIBRATION", unit=unit, completed=attempts - 1, total=budget)
                try:
                    current = self.db.model(market, symbol, hz)
                    if current is None:
                        # There is nothing to challenge yet. The first valid model
                        # becomes the initial Champion, then future changes must win
                        # a paired forward arena before replacing it.
                        result = self.lab.calibrate(market, symbol, hz, now)
                        saved = self.db.model(market, symbol, hz)
                        self.governance.mark_research(
                            market, symbol, hz, model_signature(saved or result), now.isoformat()
                        )
                        done.append({**result, "governance_status": "INITIAL_CHAMPION"})
                        continue

                    candidate, pack = self._candidate_model(market, symbol, hz, now)
                    governance = self.governance.register_challenge(current, candidate, now.isoformat())
                    if governance.get("status") == "SAME_MODEL":
                        # Same strategy+params: refresh its evidence/diagnostics but
                        # do not create a pointless challenger arena.
                        self.db.save_model(candidate)
                    done.append({
                        "market": market, "symbol": str(symbol).upper(), "horizon": hz,
                        "fetched": int(pack.get("fetched", 0) or 0),
                        "api_called": bool(pack.get("api_called", False)),
                        "strategy": candidate.get("strategy"),
                        "oos_score": candidate.get("oos_score"),
                        "calibration_score": candidate.get("calibration_score"),
                        "governance_status": governance.get("status"),
                        "arena_id": governance.get("arena_id"),
                    })
                except Exception as e:
                    required = _waiting_history_error(e)
                    row = {"market": market, "symbol": symbol, "horizon": hz}
                    if required is not None:
                        waiting.append({**row, "required_closed_bars": required, "status": "WAITING_FOR_HISTORY"})
                    else:
                        errors.append({**row, "error": f"{type(e).__name__}: {e}"})
                finally:
                    notify_progress(progress, "CALIBRATION", unit=unit, completed=attempts, total=budget)
        return {"calibrated": len(done), "waiting_history": waiting, "errors": errors,
                "budget": budget, "budget_exhausted": False, "forced": bool(force), "results": done}

    def _run_ready_once(self, now=None, progress=None):
        checked = processed = fetched = api_calls = 0
        errors = []
        skipped_unready = 0
        assets = [a for a in self.db.assets() if a.get("market") == "crypto"]
        total = len(assets) * len(_HORIZONS)
        completed = 0
        for a in assets:
            for hz in _HORIZONS:
                unit = f"{a['market']}:{a['symbol']}:{hz}"
                if self.db.model(a["market"], a["symbol"], hz) is None:
                    skipped_unready += 1
                    completed += 1
                    notify_progress(progress, "SIMULATION", unit=unit, completed=completed, total=total)
                    continue
                notify_progress(progress, "SIMULATION", unit=unit, completed=completed, total=total)
                checked += 1
                unit_started = time.monotonic()
                unit_result = {}
                try:
                    r = self.lab.process_asset_horizon(a["market"], a["symbol"], hz, now)
                    unit_result = r
                    processed += int(r.get("processed", 0))
                    fetched += int(r.get("fetched", 0))
                    api_calls += int(bool(r.get("api_called", False)))
                except Exception as e:
                    errors.append({"market": a["market"], "symbol": a["symbol"], "horizon": hz,
                                   "error": f"{type(e).__name__}: {e}"})
                finally:
                    completed += 1
                    notify_progress(progress, "SIMULATION", unit=unit, completed=completed, total=total,
                                    unit_seconds=time.monotonic() - unit_started,
                                    unit_bars=int(unit_result.get("processed", 0)),
                                    metrics={"assets_checked": checked, "bars_processed": processed,
                                             "market_data_api_calls": api_calls})
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

    def full_cycle(self, now=None, force_recalibrate=False, progress=None):
        notify_progress(progress, "PREPARE")
        imported = self.import_active()
        notify_progress(progress, "UNIVERSE")
        universe = self.universe.refresh_due(self._pinned_universe(), force=False)
        notify_progress(progress, "CALIBRATION")
        cal = self.calibrate_due(now, force_recalibrate, progress=progress)
        notify_progress(progress, "SIMULATION")
        run = self._run_ready_once(now, progress=progress)
        notify_progress(progress, "GOVERNANCE")
        governance = self.governance.process_active(self.db, self.cache, now, progress=progress)
        run_errors = run.get("errors") or []
        governance_errors = governance.get("errors") or []
        true_errors = [*cal.get("errors", []), *run_errors, *governance_errors]
        notify_progress(progress, "MODEL_HEALTH")
        health = self.model_health()
        waiting = cal.get("waiting_history", [])
        return {
            "status": "OK" if not true_errors else "PARTIAL",
            "imported": imported,
            "universe": universe,
            "calibration": cal,
            "simulation": run,
            "governance": governance,
            "forward_store": self.forward_health(),
            "health": {**health, "waiting_history": len(waiting), "true_errors": len(true_errors)},
            "true_errors": true_errors,
            "broker_order_api_calls": 0,
        }
