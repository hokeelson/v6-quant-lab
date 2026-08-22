from __future__ import annotations
from datetime import datetime, timezone
import re
import pandas as pd
from .forward_db import ForwardDB
from .simulation_db import SimulationDB
from .market_cache import MarketCache
from .simulation_engine import SimulationLab
from .paths import db_path

RECALIBRATE_HOURS={"short":72,"medium":168,"long":336}
_HORIZONS=("short","medium","long")


def _waiting_history_error(exc: Exception):
    """Return required closed-bar count when this is a normal history-shortage condition."""
    text=str(exc)
    m=re.search(r"Need at least\s+(\d+)\s+closed bars", text, flags=re.I)
    return int(m.group(1)) if m else None


class AutoOrchestrator:
    def __init__(self, initial_equity=100000.0):
        self.forward=ForwardDB(db_path("forward_validation.sqlite3"))
        self.db=SimulationDB(db_path("simulation_lab.sqlite3"))
        self.cache=MarketCache(db_path("market_cache.sqlite3"))
        self.lab=SimulationLab(self.db,self.cache,initial_equity=float(initial_equity))

    def import_active(self):
        return self.lab.import_assets(self.forward.candidates("ACTIVE"))

    def _model_due(self,market,symbol,horizon,now):
        m=self.db.model(market,symbol,horizon)
        if m is None:return True
        try:
            t=pd.Timestamp(m.get("updated_at")); t=t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            return (now-t).total_seconds() >= RECALIBRATE_HOURS[horizon]*3600
        except Exception:return True

    def model_health(self):
        assets=self.db.assets()
        total=len(assets)*len(_HORIZONS)
        ready=0
        for a in assets:
            for hz in _HORIZONS:
                if self.db.model(a["market"],a["symbol"],hz) is not None:
                    ready+=1
        return {"total_pairs":total,"ready_pairs":ready,"unready_pairs":max(0,total-ready)}

    def calibrate_due(self, now=None, force=False):
        now=pd.Timestamp(now or datetime.now(timezone.utc)); now=now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        done=[]; errors=[]; waiting=[]
        for a in self.db.assets():
            for hz in _HORIZONS:
                if not force and not self._model_due(a["market"],a["symbol"],hz,now):continue
                try:
                    done.append(self.lab.calibrate(a["market"],a["symbol"],hz,now))
                except Exception as e:
                    required=_waiting_history_error(e)
                    row={"market":a["market"],"symbol":a["symbol"],"horizon":hz}
                    if required is not None:
                        waiting.append({**row,"required_closed_bars":required,"status":"WAITING_FOR_HISTORY"})
                    else:
                        errors.append({**row,"error":f"{type(e).__name__}: {e}"})
        return {"calibrated":len(done),"waiting_history":waiting,"errors":errors}

    def full_cycle(self, now=None, force_recalibrate=False):
        imported=self.import_active()
        cal=self.calibrate_due(now,force_recalibrate)
        run=self.lab.run_once(now)
        run_errors=run.get("errors") or []
        true_errors=[*cal.get("errors",[]),*run_errors]
        health=self.model_health()
        waiting=cal.get("waiting_history",[])
        # History shortage is expected data readiness, not a system failure.
        status="OK" if not true_errors else "PARTIAL"
        return {
            "status":status,
            "imported":imported,
            "calibration":cal,
            "simulation":run,
            "health":{
                **health,
                "waiting_history":len(waiting),
                "true_errors":len(true_errors),
            },
            "true_errors":true_errors,
            "broker_order_api_calls":0,
        }
