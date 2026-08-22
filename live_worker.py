from __future__ import annotations
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from src.auto_orchestrator import AutoOrchestrator

POLL_SECONDS=60
load_dotenv()
engine=AutoOrchestrator(initial_equity=100000.0)
print("V6 Stage 7 Auto Simulation Worker started.")
print("Broker order API = 0. Market-data API is throttled and cached locally.")
print("Dashboard reads SQLite only; it does not trigger API calls every refresh.")
while True:
    stamp=datetime.now(timezone.utc).isoformat()
    try:
        r=engine.full_cycle()
        print(stamp,r,flush=True)
    except Exception as e:
        print(stamp,"AUTO_ERROR",type(e).__name__,e,flush=True)
    time.sleep(POLL_SECONDS)
