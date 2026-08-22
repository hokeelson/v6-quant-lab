from __future__ import annotations
from datetime import datetime, timezone
import pandas as pd
from .forward_db import ForwardDB
from .simulation_db import SimulationDB
from .market_cache import MarketCache
from .simulation_engine import SimulationLab
from .paths import db_path

RECALIBRATE_HOURS={"short":72,"medium":168,"long":336}

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

    def calibrate_due(self, now=None, force=False):
        now=pd.Timestamp(now or datetime.now(timezone.utc)); now=now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        done=[]; errors=[]
        for a in self.db.assets():
            for hz in ("short","medium","long"):
                if not force and not self._model_due(a["market"],a["symbol"],hz,now):continue
                try: done.append(self.lab.calibrate(a["market"],a["symbol"],hz,now))
                except Exception as e: errors.append({"market":a["market"],"symbol":a["symbol"],"horizon":hz,"error":f"{type(e).__name__}: {e}"})
        return {"calibrated":len(done),"errors":errors}

    def full_cycle(self, now=None, force_recalibrate=False):
        imported=self.import_active()
        cal=self.calibrate_due(now,force_recalibrate)
        run=self.lab.run_once(now)
        return {"status":"OK" if not cal["errors"] and not run.get("errors") else "PARTIAL","imported":imported,"calibration":cal,"simulation":run,"broker_order_api_calls":0}
