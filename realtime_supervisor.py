from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir, db_path

CHECK_SECONDS = 10
HEARTBEAT_STALE_SECONDS = 30
QUOTE_WARN_SECONDS = 30
QUOTE_RESTART_SECONDS = 90
RESTART_DELAY_SECONDS = 3
STATUS_PATH = Path(data_dir()) / "realtime_status.json"
STALE_STATUS_PATH = Path(data_dir()) / "realtime_stale_quotes.json"
REALTIME_DB_PATH = db_path("realtime_execution.sqlite3")


def _utc_now():
    return datetime.now(timezone.utc)


def _age_seconds(raw):
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (_utc_now() - t.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _heartbeat_age():
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return _age_seconds(payload.get("heartbeat_at"))
    except Exception:
        return None


def _crypto_quote_health():
    """Inspect current crypto watchlist one symbol at a time.

    A healthy websocket can still leave one symbol frozen while other symbols keep
    producing traffic, so stream heartbeat alone is not sufficient.
    """
    result = {"checked_at": _utc_now().isoformat(), "warning": [], "restart": [], "fresh": 0}
    try:
        con = sqlite3.connect(REALTIME_DB_PATH, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT w.symbol, q.ts
            FROM watchlist w
            LEFT JOIN quotes q ON q.market=w.market AND q.symbol=w.symbol
            WHERE w.market='crypto'
            ORDER BY w.symbol
        """).fetchall()
        con.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        age = _age_seconds(row["ts"])
        item = {"symbol": symbol, "age_seconds": None if age is None else int(age), "quote_ts": row["ts"]}
        if age is None or age >= QUOTE_RESTART_SECONDS:
            result["restart"].append(item)
        elif age >= QUOTE_WARN_SECONDS:
            result["warning"].append(item)
        else:
            result["fresh"] += 1
    return result


def _write_quote_health(payload):
    try:
        tmp = STALE_STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STALE_STATUS_PATH)
    except Exception as exc:
        print("REALTIME_STALE_STATUS_ERROR", type(exc).__name__, exc, flush=True)


def _stop(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    print("V6 Realtime Supervisor started.", flush=True)
    print(
        f"Per-symbol crypto stale guard: warn>{QUOTE_WARN_SECONDS}s, restart>{QUOTE_RESTART_SECONDS}s.",
        flush=True,
    )
    while True:
        started = time.time()
        proc = subprocess.Popen([sys.executable, "realtime_worker.py"])
        print(f"Realtime worker launched pid={proc.pid}", flush=True)
        stale = False
        while proc.poll() is None:
            time.sleep(CHECK_SECONDS)
            age = _heartbeat_age()
            if age is None and time.time() - started < HEARTBEAT_STALE_SECONDS:
                continue
            if age is None or age > HEARTBEAT_STALE_SECONDS:
                stale = True
                print(
                    f"Realtime heartbeat stale ({age if age is not None else 'missing'}s); restarting",
                    flush=True,
                )
                _stop(proc)
                break

            # Give a newly launched worker enough time to subscribe and receive its
            # first book/trade messages before judging individual symbols.
            if time.time() - started < QUOTE_WARN_SECONDS:
                continue

            health = _crypto_quote_health()
            _write_quote_health(health)
            warnings = health.get("warning") or []
            restarts = health.get("restart") or []
            if warnings:
                print(
                    "CRYPTO_QUOTES_STALE_WARNING",
                    ", ".join(f"{x['symbol']}={x['age_seconds']}s" for x in warnings),
                    flush=True,
                )
            if restarts:
                stale = True
                detail = ", ".join(
                    f"{x['symbol']}={x['age_seconds'] if x['age_seconds'] is not None else 'missing'}s"
                    for x in restarts
                )
                print(f"CRYPTO_QUOTES_STALE_RESTART {detail}; rebuilding Binance subscriptions", flush=True)
                _stop(proc)
                break
        if not stale:
            print(f"Realtime worker exited code={proc.poll()}; restarting", flush=True)
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
