from __future__ import annotations

import time

from src.external_intelligence import write_daily_external_intelligence

REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 15 * 60


def main():
    while True:
        try:
            payload = write_daily_external_intelligence()
            print(
                "EXTERNAL_INTELLIGENCE",
                payload.get("status"),
                payload.get("generated_at"),
                "coverage=",
                (payload.get("sources") or {}).get("source_coverage"),
                flush=True,
            )
            time.sleep(REFRESH_SECONDS)
        except Exception as exc:
            print(f"EXTERNAL_INTELLIGENCE_ERROR {type(exc).__name__}: {exc}", flush=True)
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
