from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from websocket import WebSocketTimeoutException, create_connection

from src.paths import data_dir, db_path
from src.realtime_layer import RealtimeDB, build_realtime_watchlist, evaluate_realtime_signal
from src.twstock_support import TaiwanSimulationDB

load_dotenv()

WATCHLIST_REFRESH_SECONDS = 30
STATUS_WRITE_SECONDS = 2
RECONNECT_SECONDS = 3
STATUS_PATH = Path(data_dir()) / "realtime_status.json"

sim_db = TaiwanSimulationDB(db_path("simulation_lab.sqlite3"))
rt_db = RealtimeDB()
lock = threading.Lock()
state = {
    "status": "STARTING",
    "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    "watchlist_total": 0,
    "crypto_stream": "STARTING",
    "stock_stream": "STARTING",
    "twstock_stream": "BAR_ONLY",
    "quotes_received": 0,
    "signals_updated": 0,
    "broker_order_api_calls": 0,
    "message": "Realtime worker starting",
}


def _write_status():
    with lock:
        state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        payload = dict(state)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def _status_loop():
    while True:
        try:
            _write_status()
        except Exception as exc:
            print("REALTIME_STATUS_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(STATUS_WRITE_SECONDS)


def _refresh_watchlist_loop():
    while True:
        try:
            rows = build_realtime_watchlist(sim_db, rt_db)
            rt_db.prune_ticks(6)
            with lock:
                state["watchlist_total"] = len(rows)
                state["message"] = "Realtime watchlist active"
                if state["status"] == "STARTING":
                    state["status"] = "ONLINE"
        except Exception as exc:
            with lock:
                state["status"] = "DEGRADED"
                state["message"] = f"watchlist: {type(exc).__name__}: {exc}"
            print("REALTIME_WATCHLIST_ERROR", type(exc).__name__, exc, flush=True)
        time.sleep(WATCHLIST_REFRESH_SECONDS)


def _watch_symbols(market):
    return [r["symbol"] for r in rt_db.watchlist(market)]


def _crypto_loop():
    current = None
    while True:
        ws = None
        try:
            symbols = tuple(sorted(_watch_symbols("crypto")))
            if not symbols:
                with lock:
                    state["crypto_stream"] = "WAITING"
                time.sleep(5)
                continue
            current = symbols
            streams = []
            for sym in symbols:
                s = sym.lower()
                streams.extend([f"{s}@trade", f"{s}@bookTicker"])
            ws = create_connection("wss://stream.binance.com:9443/ws", timeout=10)
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))
            ws.settimeout(5)
            with lock:
                state["crypto_stream"] = "ONLINE"
            books = {}
            while True:
                new_symbols = tuple(sorted(_watch_symbols("crypto")))
                if new_symbols != current:
                    break
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    try:
                        ws.ping()
                    except Exception:
                        break
                    continue
                if not raw:
                    continue
                msg = json.loads(raw)
                if "result" in msg:
                    continue
                et = msg.get("e")
                sym = str(msg.get("s") or "").upper()
                if not sym:
                    continue
                ts_ms = msg.get("T") or msg.get("E")
                ts = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).isoformat() if ts_ms else datetime.now(timezone.utc).isoformat()
                if et == "trade":
                    price = float(msg.get("p"))
                    b = books.get(sym, {})
                    q = rt_db.upsert_quote("crypto", sym, price=price, bid=b.get("bid"), ask=b.get("ask"), source="BINANCE_STREAM", ts=ts)
                elif et == "bookTicker" or ("b" in msg and "a" in msg):
                    bid = float(msg.get("b"))
                    ask = float(msg.get("a"))
                    books[sym] = {"bid": bid, "ask": ask}
                    q = rt_db.upsert_quote("crypto", sym, bid=bid, ask=ask, source="BINANCE_STREAM", ts=ts)
                else:
                    continue
                evaluate_realtime_signal(sim_db, rt_db, "crypto", sym, q)
                with lock:
                    state["quotes_received"] += 1
                    state["signals_updated"] += 1
        except Exception as exc:
            with lock:
                state["crypto_stream"] = "ERROR"
                state["message"] = f"crypto stream: {type(exc).__name__}: {exc}"
            print("CRYPTO_STREAM_ERROR", type(exc).__name__, exc, flush=True)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        time.sleep(RECONNECT_SECONDS)


def _stock_loop():
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    if not key or not secret:
        with lock:
            state["stock_stream"] = "NO_KEYS"
        return
    current = None
    while True:
        ws = None
        try:
            symbols = tuple(sorted(_watch_symbols("stock")))
            if not symbols:
                with lock:
                    state["stock_stream"] = "WAITING"
                time.sleep(5)
                continue
            current = symbols
            ws = create_connection("wss://stream.data.alpaca.markets/v2/iex", timeout=10)
            ws.settimeout(5)
            ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
            auth = json.loads(ws.recv())
            if not isinstance(auth, list) or not any(x.get("T") == "success" for x in auth if isinstance(x, dict)):
                raise RuntimeError(f"Alpaca stream auth failed: {auth}")
            ws.send(json.dumps({"action": "subscribe", "trades": list(symbols), "quotes": list(symbols)}))
            with lock:
                state["stock_stream"] = "ONLINE"
            latest = {}
            while True:
                new_symbols = tuple(sorted(_watch_symbols("stock")))
                if new_symbols != current:
                    break
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    continue
                if not raw:
                    continue
                msgs = json.loads(raw)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                for msg in msgs:
                    if not isinstance(msg, dict):
                        continue
                    typ = msg.get("T")
                    sym = str(msg.get("S") or "").upper()
                    if not sym:
                        continue
                    ts = str(msg.get("t") or datetime.now(timezone.utc).isoformat())
                    cur = latest.setdefault(sym, {})
                    if typ == "t" and msg.get("p") is not None:
                        cur["price"] = float(msg["p"])
                    elif typ == "q":
                        if msg.get("bp") is not None:
                            cur["bid"] = float(msg["bp"])
                        if msg.get("ap") is not None:
                            cur["ask"] = float(msg["ap"])
                    else:
                        continue
                    q = rt_db.upsert_quote("stock", sym, price=cur.get("price"), bid=cur.get("bid"), ask=cur.get("ask"), source="ALPACA_IEX_STREAM", ts=ts)
                    evaluate_realtime_signal(sim_db, rt_db, "stock", sym, q)
                    with lock:
                        state["quotes_received"] += 1
                        state["signals_updated"] += 1
        except Exception as exc:
            with lock:
                state["stock_stream"] = "ERROR"
                state["message"] = f"stock stream: {type(exc).__name__}: {exc}"
            print("STOCK_STREAM_ERROR", type(exc).__name__, exc, flush=True)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        time.sleep(RECONNECT_SECONDS)


threading.Thread(target=_status_loop, daemon=True).start()
threading.Thread(target=_refresh_watchlist_loop, daemon=True).start()
threading.Thread(target=_crypto_loop, daemon=True).start()
threading.Thread(target=_stock_loop, daemon=True).start()

print("V6 Realtime Execution Layer started.", flush=True)
print("Realtime layer is shadow-only; broker order API = 0.", flush=True)

while True:
    time.sleep(60)
