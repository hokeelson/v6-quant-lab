from pathlib import Path


def replace_once(path: str, old: str, new: str):
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    if old not in s:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    if s.count(old) != 1:
        raise SystemExit(f"anchor not unique in {path}: count={s.count(old)}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")


# 1) Atomic execution primitives in SimulationDB.
replace_once(
    "src/simulation_db.py",
    '''    def add_trade(self,t):\n        x=dict(t); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso())\n        with self._c() as c:\n            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)""",x)\n''',
    '''    def fill_buy_atomic(self, aid, oid, bar, price, fees, slippage, new_cash, position):\n        p=dict(position)\n        with self._c() as c:\n            c.execute("BEGIN IMMEDIATE")\n            cur=c.execute("UPDATE orders SET status='FILLED',filled_bar=?,fill_price=?,fees=?,slippage_cost=? WHERE order_id=? AND status='PENDING'",\n                          (bar,float(price),float(fees),float(slippage),oid))\n            if cur.rowcount != 1:\n                return False\n            c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(new_cash),aid))\n            c.execute("""INSERT INTO positions(account_id,symbol,qty,avg_entry,entry_bar,strategy,horizon,regime_entry,stop_price,target_price,max_holding_bars,bars_held,leverage_at_entry)\n            VALUES(:account_id,:symbol,:qty,:avg_entry,:entry_bar,:strategy,:horizon,:regime_entry,:stop_price,:target_price,:max_holding_bars,:bars_held,:leverage_at_entry)\n            ON CONFLICT(account_id,symbol) DO UPDATE SET qty=excluded.qty,avg_entry=excluded.avg_entry,entry_bar=excluded.entry_bar,\n            strategy=excluded.strategy,horizon=excluded.horizon,regime_entry=excluded.regime_entry,stop_price=excluded.stop_price,target_price=excluded.target_price,\n            max_holding_bars=excluded.max_holding_bars,bars_held=excluded.bars_held,leverage_at_entry=excluded.leverage_at_entry""",p)\n        return True\n\n    def fill_sell_atomic(self, aid, oid, bar, price, fees, slippage, new_cash, trade, symbol):\n        x=dict(trade); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso())\n        with self._c() as c:\n            c.execute("BEGIN IMMEDIATE")\n            cur=c.execute("UPDATE orders SET status='FILLED',filled_bar=?,fill_price=?,fees=?,slippage_cost=? WHERE order_id=? AND status='PENDING'",\n                          (bar,float(price),float(fees),float(slippage),oid))\n            if cur.rowcount != 1:\n                return False\n            c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(new_cash),aid))\n            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)""",x)\n            c.execute("DELETE FROM positions WHERE account_id=? AND symbol=?",(aid,str(symbol).upper()))\n        return True\n\n    def close_position_atomic(self, aid, new_cash, trade, symbol):\n        x=dict(trade); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso())\n        with self._c() as c:\n            c.execute("BEGIN IMMEDIATE")\n            exists=c.execute("SELECT 1 FROM positions WHERE account_id=? AND symbol=?",(aid,str(symbol).upper())).fetchone()\n            if not exists:\n                return False\n            c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(new_cash),aid))\n            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)""",x)\n            c.execute("DELETE FROM positions WHERE account_id=? AND symbol=?",(aid,str(symbol).upper()))\n        return True\n\n    def add_trade(self,t):\n        x=dict(t); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso())\n        with self._c() as c:\n            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)""",x)\n'''
)

# 2) Base BUY becomes atomic.
replace_once(
    "src/simulation_engine.py",
    '''            self.db.set_cash(aid,cash-notional)\n            self.db.upsert_position({"account_id":aid,"symbol":symbol,"qty":qty,"avg_entry":fill,"entry_bar":ts.isoformat(),\n                "strategy":decision_context.get("strategy"),"horizon":hz,"regime_entry":decision_context.get("regime"),\n                "stop_price":fill*(1-float(decision_context.get("stop_distance",0.08))),"target_price":fill*(1+float(decision_context.get("target_distance",0.20))),\n                "max_holding_bars":int(decision_context.get("diagnostics",{}).get("max_holding_bars",HORIZON_SPECS[hz]["max_holding_stock"] if market=="stock" else HORIZON_SPECS[hz]["max_holding_crypto"])),"bars_held":0,"leverage_at_entry":float(decision_context.get("leverage",1.0))})\n            self.db.fill_order(o["order_id"],ts.isoformat(),fill,fees,fill-open_px)\n''',
    '''            position={"account_id":aid,"symbol":symbol,"qty":qty,"avg_entry":fill,"entry_bar":ts.isoformat(),\n                "strategy":decision_context.get("strategy"),"horizon":hz,"regime_entry":decision_context.get("regime"),\n                "stop_price":fill*(1-float(decision_context.get("stop_distance",0.08))),"target_price":fill*(1+float(decision_context.get("target_distance",0.20))),\n                "max_holding_bars":int(decision_context.get("diagnostics",{}).get("max_holding_bars",HORIZON_SPECS[hz]["max_holding_stock"] if market=="stock" else HORIZON_SPECS[hz]["max_holding_crypto"])),"bars_held":0,"leverage_at_entry":float(decision_context.get("leverage",1.0))}\n            if not self.db.fill_buy_atomic(aid,o["order_id"],ts.isoformat(),fill,fees,fill-open_px,cash-notional,position):\n                return None\n'''
)

# 3) Base SELL becomes atomic.
replace_once(
    "src/simulation_engine.py",
    '''            self.db.set_cash(aid,cash); self.db.fill_order(o["order_id"],ts.isoformat(),fill,proceeds*rate,open_px-fill)\n            self.db.add_trade({"account_id":aid,"symbol":symbol,"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,\n                "strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":o["reason"] or "SIGNAL_EXIT","leverage":pos["leverage_at_entry"]})\n''',
    '''            trade={"account_id":aid,"symbol":symbol,"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,\n                "strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":o["reason"] or "SIGNAL_EXIT","leverage":pos["leverage_at_entry"]}\n            if not self.db.fill_sell_atomic(aid,o["order_id"],ts.isoformat(),fill,proceeds*rate,open_px-fill,cash,trade,symbol):\n                return None\n'''
)
replace_once("src/simulation_engine.py", '''            self.db.delete_position(aid,symbol); return "SELL"\n''', '''            return "SELL"\n''')

# 4) Gap-aware stop + atomic protective exit.
replace_once(
    "src/simulation_engine.py",
    '''        # conservative same-bar tie-break: stop before target\n        if float(row.low)<=float(pos["stop_price"]): exit_px=float(pos["stop_price"]); reason="ATR_STOP"\n        elif float(row.high)>=float(pos["target_price"]): exit_px=float(pos["target_price"]); reason="ATR_TARGET"\n''',
    '''        # conservative same-bar tie-break: stop before target. If price gaps below\n        # the stop, the stop cannot fill at the stale trigger price; use the open.\n        if float(row.open)<=float(pos["stop_price"]): exit_px=float(row.open); reason="ATR_STOP_GAP"\n        elif float(row.low)<=float(pos["stop_price"]): exit_px=float(pos["stop_price"]); reason="ATR_STOP"\n        elif float(row.high)>=float(pos["target_price"]): exit_px=float(pos["target_price"]); reason="ATR_TARGET"\n'''
)
replace_once(
    "src/simulation_engine.py",
    '''        self.db.set_cash(aid,float(acct["cash"])+proceeds)\n        self.db.add_trade({"account_id":aid,"symbol":symbol,"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,\n            "strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":reason,"leverage":pos["leverage_at_entry"]})\n        self.db.delete_position(aid,symbol)\n''',
    '''        trade={"account_id":aid,"symbol":symbol,"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,\n            "strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":reason,"leverage":pos["leverage_at_entry"]}\n        if not self.db.close_position_atomic(aid,float(acct["cash"])+proceeds,trade,symbol):\n            return None\n'''
)

# 5) Atomic margin liquidation.
replace_once(
    "src/simulation_engine.py",
    '''            acct=self.db.account(aid); self.db.set_cash(aid,float(acct["cash"])+proceeds)\n            pnl=float(pos["qty"])*(fill-float(pos["avg_entry"])); ret=fill/float(pos["avg_entry"])-1\n            self.db.add_trade({"account_id":aid,"symbol":pos["symbol"],"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,"strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":"MARGIN_LIQUIDATION","leverage":pos["leverage_at_entry"]})\n            self.db.add_diagnostic(aid,pos["symbol"],pos["horizon"],ts.isoformat(),"LIQUIDATION","Maintenance margin breached",{"margin_ratio":ratio,"maintenance":maintenance,"pnl":pnl})\n            self.db.delete_position(aid,pos["symbol"])\n''',
    '''            acct=self.db.account(aid)\n            pnl=float(pos["qty"])*(fill-float(pos["avg_entry"])); ret=fill/float(pos["avg_entry"])-1\n            trade={"account_id":aid,"symbol":pos["symbol"],"entry_bar":pos["entry_bar"],"exit_bar":ts.isoformat(),"qty":pos["qty"],"entry_price":pos["avg_entry"],"exit_price":fill,"realized_pnl":pnl,"return_pct":ret,"strategy":pos["strategy"],"horizon":pos["horizon"],"regime_entry":pos.get("regime_entry"),"exit_reason":"MARGIN_LIQUIDATION","leverage":pos["leverage_at_entry"]}\n            if not self.db.close_position_atomic(aid,float(acct["cash"])+proceeds,trade,pos["symbol"]):\n                continue\n            self.db.add_diagnostic(aid,pos["symbol"],pos["horizon"],ts.isoformat(),"LIQUIDATION","Maintenance margin breached",{"margin_ratio":ratio,"maintenance":maintenance,"pnl":pnl})\n'''
)

# 6) Taiwan execution gets the same stale-order and zero-size safety.
replace_once(
    "src/twstock_support.py",
    '''        rate = self._cost_rate(market)\n        open_px = float(row.open)\n        if o["side"] == "BUY" and pos is None:\n            cash, gross, equity = self._account_marks(aid, {symbol: open_px})\n            hz = decision_context.get("horizon", aid.rsplit("_", 1)[-1])\n''',
    '''        rate = self._cost_rate(market)\n        open_px = float(row.open)\n        hz = decision_context.get("horizon", aid.rsplit("_", 1)[-1])\n        if o["side"] == "BUY" and pos is not None:\n            self.db.cancel_order(o["order_id"], "STALE_BUY_POSITION_EXISTS")\n            self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "ORDER_CANCELLED", "Stale BUY cancelled because position already exists",\n                                   {"cancel_reason": "STALE_BUY_POSITION_EXISTS", "broker_order_api_calls": 0})\n            return "CANCELLED"\n        if o["side"] == "SELL" and pos is None:\n            self.db.cancel_order(o["order_id"], "STALE_SELL_NO_POSITION")\n            self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "ORDER_CANCELLED", "Stale SELL cancelled because position no longer exists",\n                                   {"cancel_reason": "STALE_SELL_NO_POSITION", "broker_order_api_calls": 0})\n            return "CANCELLED"\n        if o["side"] == "BUY" and pos is None:\n            cash, gross, equity = self._account_marks(aid, {symbol: open_px})\n'''
)
replace_once(
    "src/twstock_support.py",
    '''            qty = math.floor(notional / fill) if fill > 0 else 0\n            if qty <= 0:\n                return None\n''',
    '''            qty = math.floor(notional / fill) if fill > 0 else 0\n            if qty <= 0:\n                cancel_reason = "RISK_SIZING_ZERO_NOTIONAL" if risk_adjusted <= 0 else "INSUFFICIENT_CASH_FOR_BOARD_LOT"\n                self.db.cancel_order(o["order_id"], cancel_reason)\n                self.db.add_diagnostic(aid, symbol, hz, ts.isoformat(), "ORDER_CANCELLED", "Pending Taiwan BUY cancelled before fill",\n                                       {**sizing, "cash_room": max(0.0, cash), "risk_adjusted_notional": risk_adjusted,\n                                        "requested_notional": original_notional, "cancel_reason": cancel_reason, "broker_order_api_calls": 0})\n                return "CANCELLED"\n'''
)
replace_once(
    "src/twstock_support.py",
    '''            self.db.set_cash(aid, cash - spent)\n            self.db.upsert_position({\n                "account_id": aid, "symbol": symbol, "qty": qty, "avg_entry": fill,\n                "entry_bar": ts.isoformat(), "strategy": decision_context.get("strategy"),\n                "horizon": hz, "regime_entry": decision_context.get("regime"),\n                "stop_price": fill * (1 - float(decision_context.get("stop_distance", 0.08))),\n                "target_price": fill * (1 + float(decision_context.get("target_distance", 0.20))),\n                "max_holding_bars": int((decision_context.get("diagnostics") or {}).get("max_holding_bars", TW_MAX_HOLDING[hz])),\n                "bars_held": 0, "leverage_at_entry": 1.0,\n            })\n            self.db.fill_order(o["order_id"], ts.isoformat(), fill, fees, fill - open_px)\n''',
    '''            position={\n                "account_id": aid, "symbol": symbol, "qty": qty, "avg_entry": fill,\n                "entry_bar": ts.isoformat(), "strategy": decision_context.get("strategy"),\n                "horizon": hz, "regime_entry": decision_context.get("regime"),\n                "stop_price": fill * (1 - float(decision_context.get("stop_distance", 0.08))),\n                "target_price": fill * (1 + float(decision_context.get("target_distance", 0.20))),\n                "max_holding_bars": int((decision_context.get("diagnostics") or {}).get("max_holding_bars", TW_MAX_HOLDING[hz])),\n                "bars_held": 0, "leverage_at_entry": 1.0,\n            }\n            if not self.db.fill_buy_atomic(aid, o["order_id"], ts.isoformat(), fill, fees, fill - open_px, cash - spent, position):\n                return None\n'''
)
replace_once(
    "src/twstock_support.py",
    '''            self.db.set_cash(aid, cash)\n            self.db.fill_order(o["order_id"], ts.isoformat(), fill, proceeds * rate, open_px - fill)\n            self.db.add_trade({\n                "account_id": aid, "symbol": symbol, "entry_bar": pos["entry_bar"],\n                "exit_bar": ts.isoformat(), "qty": pos["qty"], "entry_price": pos["avg_entry"],\n                "exit_price": fill, "realized_pnl": pnl, "return_pct": ret,\n                "strategy": pos["strategy"], "horizon": pos["horizon"],\n                "regime_entry": pos.get("regime_entry"), "exit_reason": o["reason"] or "SIGNAL_EXIT",\n                "leverage": 1.0,\n            })\n''',
    '''            trade={\n                "account_id": aid, "symbol": symbol, "entry_bar": pos["entry_bar"],\n                "exit_bar": ts.isoformat(), "qty": pos["qty"], "entry_price": pos["avg_entry"],\n                "exit_price": fill, "realized_pnl": pnl, "return_pct": ret,\n                "strategy": pos["strategy"], "horizon": pos["horizon"],\n                "regime_entry": pos.get("regime_entry"), "exit_reason": o["reason"] or "SIGNAL_EXIT",\n                "leverage": 1.0,\n            }\n            if not self.db.fill_sell_atomic(aid, o["order_id"], ts.isoformat(), fill, proceeds * rate, open_px - fill, cash, trade, symbol):\n                return None\n'''
)
replace_once("src/twstock_support.py", '''            self.db.delete_position(aid, symbol)\n            return "SELL"\n''', '''            return "SELL"\n''')

# 7) Persistence window: 60 seconds default instead of 5 minutes.
replace_once(
    "storage_rescue.py",
    'INTERVAL = max(60, int(os.getenv("V6_SNAPSHOT_INTERVAL_SECONDS", "300")))',
    'INTERVAL = max(60, int(os.getenv("V6_SNAPSHOT_INTERVAL_SECONDS", "60")))'
)

# 8) Focused regression tests.
Path("tests/test_simulation_execution_safety.py").write_text(r'''import pandas as pd

from src.simulation_db import SimulationDB
from src.simulation_engine import SimulationLab


def _position(aid="stock_short", symbol="TEST"):
    return {
        "account_id": aid, "symbol": symbol, "qty": 10.0, "avg_entry": 100.0,
        "entry_bar": "2026-01-01T00:00:00+00:00", "strategy": "trend_ma", "horizon": "short",
        "regime_entry": "bull", "stop_price": 90.0, "target_price": 130.0,
        "max_holding_bars": 100, "bars_held": 0, "leverage_at_entry": 1.0,
    }


def test_stale_buy_is_cancelled(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3"))
    db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "BUY", "created_bar": "2026-01-01T00:00:00+00:00", "requested_notional": 1000, "qty": None, "reason": "TEST", "decision_id": None})
    lab = SimulationLab(db=db)
    result = lab._execute_pending(aid, "stock", symbol, pd.Timestamp("2026-01-02", tz="UTC"), pd.Series({"open": 100.0}))
    assert result == "CANCELLED"
    with db._c() as c:
        row = c.execute("SELECT status,reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert row["status"] == "CANCELLED"
    assert row["reason"] == "STALE_BUY_POSITION_EXISTS"


def test_gap_stop_uses_open_and_closes_atomically(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3"))
    db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    lab = SimulationLab(db=db)
    result = lab._protect_position(aid, "stock", symbol, pd.Timestamp("2026-01-02", tz="UTC"), pd.Series({"open": 80.0, "high": 85.0, "low": 75.0, "close": 82.0}))
    assert result == "ATR_STOP_GAP"
    assert db.position(aid, symbol) is None
    trades = db.recent_trades(10)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "ATR_STOP_GAP"
    assert trades[0]["exit_price"] < 81.0


def test_atomic_sell_is_idempotent(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3"))
    db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00", "requested_notional": 0, "qty": 10, "reason": "TEST", "decision_id": None})
    trade = {"account_id": aid, "symbol": symbol, "entry_bar": "2026-01-01T00:00:00+00:00", "exit_bar": "2026-01-02T00:00:00+00:00", "qty": 10, "entry_price": 100, "exit_price": 110, "realized_pnl": 100, "return_pct": 0.1, "strategy": "trend_ma", "horizon": "short", "regime_entry": "bull", "exit_reason": "TEST", "leverage": 1.0}
    assert db.fill_sell_atomic(aid, oid, trade["exit_bar"], 110, 0, 0, 101100, trade, symbol) is True
    assert db.fill_sell_atomic(aid, oid, trade["exit_bar"], 110, 0, 0, 102200, trade, symbol) is False
    assert len(db.recent_trades(10)) == 1
    assert db.position(aid, symbol) is None
''', encoding="utf-8")

# 9) Permanent CI, not a one-shot patch workflow.
Path(".github/workflows/ci.yml").write_text('''name: CI\n\non:\n  push:\n    branches: [main]\n  pull_request:\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n          cache: pip\n      - run: python -m pip install --upgrade pip\n      - run: pip install -r requirements.txt pytest\n      - run: pytest -q\n''', encoding="utf-8")

print("execution core stabilization patch applied")
