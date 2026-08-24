from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.paths import data_dir, db_path
from src.realtime_layer import RealtimeDB

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


def _binance_rest_refresh(symbol: str) -> dict:
    """Refresh one stale spot symbol using Binance public REST bookTicker.

    This is a safety fallback only; normal realtime remains WebSocket-driven.
    """
    url = "https://api.binance.com/api/v3/ticker/bookTicker?" + urllib.parse.urlencode({"symbol": symbol})
    req = urllib.request.Request(url, headers={"User-Agent": "V6-Quant-Lab/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        bid = float(payload["bidPrice"])
        ask = float(payload["askPrice"])
        price = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (bid or ask)
        rt = RealtimeDB(REALTIME_DB_PATH)
        rt.upsert_quote(
            "crypto", symbol, price=price, bid=bid, ask=ask,
            source="BINANCE_REST_FALLBACK", ts=_utc_now().isoformat(),
        )
        return {"ok": True, "symbol": symbol, "bid": bid, "ask": ask}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        invalid = exc.code in (400, 404) and ("Invalid symbol" in body or "-1121" in body)
        return {
            "ok": False, "symbol": symbol, "invalid_symbol": invalid,
            "error": f"HTTP {exc.code}: {body[:200]}",
        }
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "invalid_symbol": False,
                "error": f"{type(exc).__name__}: {exc}"}


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
        f"Per-symbol crypto stale guard: warn>{QUOTE_WARN_SECONDS}s, REST fallback first, restart>{QUOTE_RESTART_SECONDS}s.",
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

            if time.time() - started < QUOTE_WARN_SECONDS:
                continue

            health = _crypto_quote_health()
            warnings = list(health.get("warning") or [])
            restarts = list(health.get("restart") or [])
            candidates = warnings + restarts
            recovered = []
            invalid = []
            failed = []

            for item in candidates:
                symbol = item["symbol"]
                result = _binance_rest_refresh(symbol)
                if result.get("ok"):
                    recovered.append({**item, "source": "BINANCE_REST_FALLBACK"})
                    print(f"CRYPTO_QUOTE_REST_RECOVERED {symbol}", flush=True)
                elif result.get("invalid_symbol"):
                    invalid.append({**item, "error": result.get("error")})
                    print(f"CRYPTO_QUOTE_INVALID_SYMBOL {symbol} {result.get('error')}", flush=True)
                else:
                    failed.append({**item, "error": result.get("error")})
                    print(f"CRYPTO_QUOTE_REST_FAILED {symbol} {result.get('error')}", flush=True)

            health["rest_recovered"] = recovered
            health["invalid_symbols"] = invalid
            health["rest_failed"] = failed
            _write_quote_health(health)

            # Only restart the whole websocket when a symbol is severely stale and
            # the REST safety fallback also failed. Invalid/delisted symbols are
            # reported but do not cause an endless reconnect loop.
            failed_symbols = {x["symbol"] for x in failed}
            restart_symbols = [x for x in restarts if x["symbol"] in failed_symbols]
            if restart_symbols:
                stale = True
                detail = ", ".join(
                    f"{x['symbol']}={x['age_seconds'] if x['age_seconds'] is not None else 'missing'}s"
                    for x in restart_symbols
                )
                print(f"CRYPTO_QUOTES_STALE_RESTART {detail}; rebuilding Binance subscriptions", flush=True)
                _stop(proc)
                break
        if not stale:
            print(f"Realtime worker exited code={proc.poll()}; restarting", flush=True)
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
