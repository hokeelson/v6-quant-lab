from __future__ import annotations
import os, time
from datetime import datetime, timezone
from dotenv import load_dotenv

from src.simulation_db import SimulationDB
from src.market_cache import MarketCache
from src.simulation_engine import SimulationLab

CHECK_EVERY_SECONDS = 3600
load_dotenv()

lab = SimulationLab(SimulationDB("simulation_lab.sqlite3"), MarketCache("market_cache.sqlite3"), initial_equity=100000.0)
print("V6 Stage 6 Local Simulation Worker started.")
print("No broker order API is used. Market-data API only fills missing cached bars.")
print("Short=1H, Medium=4H, Long=1D. Signal at bar close -> earliest fill next bar open.")
while True:
    stamp=datetime.now(timezone.utc).isoformat()
    try:
        r=lab.run_once()
        print(stamp,"SIMULATION",r,flush=True)
    except Exception as e:
        print(stamp,"SIMULATION_ERROR",type(e).__name__,e,flush=True)
    time.sleep(CHECK_EVERY_SECONDS)
