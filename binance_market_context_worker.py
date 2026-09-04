from __future__ import annotations

import time

from src.binance_market_context import write_snapshot
from src.paths import db_path
from src.simulation_db import SimulationDB

REFRESH_SECONDS = 60


def current_crypto_symbols(db: SimulationDB) -> list[str]:
    symbols = []
    for row in db.assets():
        if str(row.get("market") or "") != "crypto":
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def main():
    db = SimulationDB(db_path("simulation_lab.sqlite3"))
    while True:
        try:
            symbols = current_crypto_symbols(db)
            payload = write_snapshot(symbols)
            print(
                "BINANCE_CONTEXT",
                payload.get("status"),
                f"symbols={payload.get('symbols', 0)}",
                f"spot={payload.get('spot_depth_coverage', 0):.0%}",
                f"futures={payload.get('futures_coverage', 0):.0%}",
                flush=True,
            )
        except Exception as exc:
            print("BINANCE_CONTEXT_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
