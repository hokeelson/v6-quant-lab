from __future__ import annotations
import json, os, sqlite3, uuid
from pathlib import Path
from datetime import datetime, timezone


def now_iso(): return datetime.now(timezone.utc).isoformat()

SCHEMA_VERSION = 2


class SimulationDB:
    def __init__(self, path: str = "simulation_lab.sqlite3"):
        self.path = path
        self._init()

    def _c(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def _init(self):
        with self._c() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
              account_id TEXT PRIMARY KEY, market TEXT NOT NULL, horizon TEXT NOT NULL,
              initial_equity REAL NOT NULL, cash REAL NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
              created_at TEXT NOT NULL, UNIQUE(market,horizon));
            CREATE TABLE IF NOT EXISTS assets(
              market TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
              added_at TEXT NOT NULL, PRIMARY KEY(market,symbol));
            CREATE TABLE IF NOT EXISTS models(
              market TEXT NOT NULL, symbol TEXT NOT NULL, horizon TEXT NOT NULL,
              strategy TEXT NOT NULL, params_json TEXT NOT NULL, calibration_score REAL NOT NULL,
              oos_score REAL NOT NULL, train_score REAL NOT NULL, regime_fit REAL NOT NULL,
              calibrated_through TEXT NOT NULL, diagnostics_json TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(market,symbol,horizon));
            CREATE TABLE IF NOT EXISTS decisions(
              decision_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, market TEXT NOT NULL,
              symbol TEXT NOT NULL, horizon TEXT NOT NULL, bar_time TEXT NOT NULL,
              action TEXT NOT NULL, confidence REAL NOT NULL, strategy TEXT,
              params_json TEXT, regime TEXT, atr_pct REAL, stop_distance REAL,
              target_distance REAL, risk_budget_pct REAL, requested_notional REAL,
              leverage REAL, reason TEXT, diagnostics_json TEXT, created_at TEXT NOT NULL,
              UNIQUE(account_id,symbol,bar_time));
            CREATE TABLE IF NOT EXISTS orders(
              order_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
              side TEXT NOT NULL, created_bar TEXT NOT NULL, status TEXT NOT NULL,
              requested_notional REAL, qty REAL, reason TEXT, decision_id TEXT,
              filled_bar TEXT, fill_price REAL, fees REAL DEFAULT 0, slippage_cost REAL DEFAULT 0,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL,
              avg_entry REAL NOT NULL, entry_bar TEXT NOT NULL, strategy TEXT, horizon TEXT, regime_entry TEXT,
              stop_price REAL, target_price REAL, max_holding_bars INTEGER,
              bars_held INTEGER NOT NULL DEFAULT 0, leverage_at_entry REAL NOT NULL DEFAULT 1,
              PRIMARY KEY(account_id,symbol));
            CREATE TABLE IF NOT EXISTS trades(
              trade_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
              entry_bar TEXT NOT NULL, exit_bar TEXT NOT NULL, qty REAL NOT NULL,
              entry_price REAL NOT NULL, exit_price REAL NOT NULL, realized_pnl REAL NOT NULL,
              return_pct REAL NOT NULL, strategy TEXT, horizon TEXT, regime_entry TEXT,
              exit_reason TEXT, leverage REAL, exit_order_id TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS equity_history(
              account_id TEXT NOT NULL, bar_time TEXT NOT NULL, equity REAL NOT NULL,
              cash REAL NOT NULL, gross_exposure REAL NOT NULL, leverage REAL NOT NULL,
              drawdown REAL NOT NULL, PRIMARY KEY(account_id,bar_time));
            CREATE TABLE IF NOT EXISTS diagnostics(
              id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, symbol TEXT, horizon TEXT,
              bar_time TEXT, category TEXT, detail TEXT, payload_json TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS marks(
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, bar_time TEXT NOT NULL, price REAL NOT NULL,
              PRIMARY KEY(account_id,symbol));
            CREATE TABLE IF NOT EXISTS engine_state(
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, last_processed_bar TEXT,
              PRIMARY KEY(account_id,symbol));
            """)
            self._migrate(c)

    def _migrate(self, c):
        current = int(c.execute("PRAGMA user_version").fetchone()[0])
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"simulation DB schema {current} is newer than supported {SCHEMA_VERSION}")
        # Multiple worker/exporter processes can initialize the same database.
        # Serialize schema inspection and ALTERs so they cannot race each other.
        c.execute("BEGIN IMMEDIATE")
        current = int(c.execute("PRAGMA user_version").fetchone()[0])
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"simulation DB schema {current} is newer than supported {SCHEMA_VERSION}")
        if current < 1:
            cols = {str(r[1]) for r in c.execute("PRAGMA table_info(trades)").fetchall()}
            if "exit_order_id" not in cols:
                c.execute("ALTER TABLE trades ADD COLUMN exit_order_id TEXT")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_exit_order_id ON trades(exit_order_id) WHERE exit_order_id IS NOT NULL")
            c.execute("PRAGMA user_version=1")
        else:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_exit_order_id ON trades(exit_order_id) WHERE exit_order_id IS NOT NULL")
        for table in ("positions", "trades"):
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if "entry_order_id" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN entry_order_id TEXT")
            c.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_entry_order ON {table}(entry_order_id)")
        c.execute("PRAGMA user_version=2")

    def _checkpoint_persistent(self):
        persist_root = os.getenv("V6_PERSISTENT_DATA_DIR")
        if not persist_root:
            return False
        src = Path(self.path)
        if src.name != "simulation_lab.sqlite3" or not src.exists():
            return False
        current = Path(persist_root) / "v6-snapshots" / "current"
        current.mkdir(parents=True, exist_ok=True)
        target = current / src.name
        tmp = current / f".{src.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        src_con = dst_con = None
        try:
            src_con = sqlite3.connect(str(src), timeout=30)
            dst_con = sqlite3.connect(str(tmp), timeout=30)
            src_con.backup(dst_con, pages=256, sleep=0.01)
            dst_con.commit()
            row = dst_con.execute("PRAGMA quick_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise sqlite3.DatabaseError("checkpoint quick_check failed")
            dst_con.close(); dst_con = None
            src_con.close(); src_con = None
            os.replace(tmp, target)
            return True
        except Exception:
            return False
        finally:
            if dst_con is not None:
                dst_con.close()
            if src_con is not None:
                src_con.close()
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def ensure_accounts(self, initial_equity: float = 100000.0):
        rows=[]
        with self._c() as c:
            for market in ("stock","crypto"):
                for horizon in ("short","medium","long"):
                    aid=f"{market}_{horizon}"
                    c.execute("INSERT OR IGNORE INTO accounts(account_id,market,horizon,initial_equity,cash,created_at) VALUES(?,?,?,?,?,?)",
                              (aid,market,horizon,float(initial_equity),float(initial_equity),now_iso()))
                    rows.append(dict(c.execute("SELECT * FROM accounts WHERE account_id=?",(aid,)).fetchone()))
        return rows

    def reset_lab(self, initial_equity: float = 100000.0):
        with self._c() as c:
            for t in ("orders","positions","trades","equity_history","decisions","diagnostics","marks","engine_state","models","accounts"):
                c.execute(f"DELETE FROM {t}")
        return self.ensure_accounts(initial_equity)

    def add_asset(self, market, symbol):
        with self._c() as c:
            c.execute("INSERT OR IGNORE INTO assets(market,symbol,added_at) VALUES(?,?,?)",(market,symbol.upper(),now_iso()))
    def assets(self):
        with self._c() as c: return [dict(x) for x in c.execute("SELECT * FROM assets WHERE status='ACTIVE' ORDER BY market,symbol")]
    def accounts(self):
        with self._c() as c: return [dict(x) for x in c.execute("SELECT * FROM accounts ORDER BY market,horizon")]
    def account(self, aid):
        with self._c() as c:
            r=c.execute("SELECT * FROM accounts WHERE account_id=?",(aid,)).fetchone(); return dict(r) if r else None
    def set_cash(self, aid, cash):
        with self._c() as c: c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(cash),aid))
    def positions(self, aid=None):
        with self._c() as c:
            q="SELECT * FROM positions"; a=[]
            if aid: q+=" WHERE account_id=?"; a=[aid]
            return [dict(x) for x in c.execute(q,a)]
    def position(self, aid,symbol):
        with self._c() as c:
            r=c.execute("SELECT * FROM positions WHERE account_id=? AND symbol=?",(aid,symbol.upper())).fetchone(); return dict(r) if r else None
    def upsert_position(self, p):
        with self._c() as c:
            c.execute("""INSERT INTO positions(account_id,symbol,qty,avg_entry,entry_bar,strategy,horizon,regime_entry,stop_price,target_price,max_holding_bars,bars_held,leverage_at_entry)
            VALUES(:account_id,:symbol,:qty,:avg_entry,:entry_bar,:strategy,:horizon,:regime_entry,:stop_price,:target_price,:max_holding_bars,:bars_held,:leverage_at_entry)
            ON CONFLICT(account_id,symbol) DO UPDATE SET qty=excluded.qty,avg_entry=excluded.avg_entry,entry_bar=excluded.entry_bar,
            strategy=excluded.strategy,horizon=excluded.horizon,regime_entry=excluded.regime_entry,stop_price=excluded.stop_price,target_price=excluded.target_price,
            max_holding_bars=excluded.max_holding_bars,bars_held=excluded.bars_held,leverage_at_entry=excluded.leverage_at_entry""",p)
    def delete_position(self, aid,symbol):
        with self._c() as c: c.execute("DELETE FROM positions WHERE account_id=? AND symbol=?",(aid,symbol.upper()))
    def save_model(self, m):
        m=dict(m); m["params_json"]=json.dumps(m.pop("params"),ensure_ascii=False,sort_keys=True); m["diagnostics_json"]=json.dumps(m.pop("diagnostics",{}),ensure_ascii=False)
        with self._c() as c:
            c.execute("""INSERT INTO models(market,symbol,horizon,strategy,params_json,calibration_score,oos_score,train_score,regime_fit,calibrated_through,diagnostics_json,updated_at)
            VALUES(:market,:symbol,:horizon,:strategy,:params_json,:calibration_score,:oos_score,:train_score,:regime_fit,:calibrated_through,:diagnostics_json,:updated_at)
            ON CONFLICT(market,symbol,horizon) DO UPDATE SET strategy=excluded.strategy,params_json=excluded.params_json,
            calibration_score=excluded.calibration_score,oos_score=excluded.oos_score,train_score=excluded.train_score,regime_fit=excluded.regime_fit,
            calibrated_through=excluded.calibrated_through,diagnostics_json=excluded.diagnostics_json,updated_at=excluded.updated_at""",m)
    def model(self,market,symbol,horizon):
        with self._c() as c:
            r=c.execute("SELECT * FROM models WHERE market=? AND symbol=? AND horizon=?",(market,symbol.upper(),horizon)).fetchone()
            if not r:return None
            d=dict(r); d["params"]=json.loads(d.pop("params_json")); d["diagnostics"]=json.loads(d.pop("diagnostics_json")); return d
    def models(self):
        with self._c() as c: return [dict(x) for x in c.execute("SELECT * FROM models ORDER BY market,symbol,horizon")]
    def last_processed(self,aid,symbol):
        with self._c() as c:
            r=c.execute("SELECT last_processed_bar FROM engine_state WHERE account_id=? AND symbol=?",(aid,symbol.upper())).fetchone(); return r[0] if r else None
    def set_last_processed(self,aid,symbol,ts):
        with self._c() as c: c.execute("INSERT INTO engine_state(account_id,symbol,last_processed_bar) VALUES(?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET last_processed_bar=excluded.last_processed_bar",(aid,symbol.upper(),ts))
    def add_decision(self,d):
        x=dict(d); x.setdefault("decision_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso()); x["params_json"]=json.dumps(x.pop("params",{}),ensure_ascii=False,sort_keys=True); x["diagnostics_json"]=json.dumps(x.pop("diagnostics",{}),ensure_ascii=False)
        with self._c() as c:
            c.execute("""INSERT OR IGNORE INTO decisions(decision_id,account_id,market,symbol,horizon,bar_time,action,confidence,strategy,params_json,regime,atr_pct,stop_distance,target_distance,risk_budget_pct,requested_notional,leverage,reason,diagnostics_json,created_at)
            VALUES(:decision_id,:account_id,:market,:symbol,:horizon,:bar_time,:action,:confidence,:strategy,:params_json,:regime,:atr_pct,:stop_distance,:target_distance,:risk_budget_pct,:requested_notional,:leverage,:reason,:diagnostics_json,:created_at)""",x)
        return x["decision_id"]

    def decision(self,did):
        with self._c() as c:
            r=c.execute("SELECT * FROM decisions WHERE decision_id=?",(did,)).fetchone()
            if not r:return None
            d=dict(r); d["params"]=json.loads(d.pop("params_json") or "{}"); d["diagnostics"]=json.loads(d.pop("diagnostics_json") or "{}"); return d
    def set_mark(self,aid,symbol,bar,price):
        with self._c() as c: c.execute("INSERT INTO marks(account_id,symbol,bar_time,price) VALUES(?,?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET bar_time=excluded.bar_time,price=excluded.price",(aid,symbol.upper(),bar,float(price)))
    def marks(self,aid):
        with self._c() as c:return {r["symbol"]:float(r["price"]) for r in c.execute("SELECT symbol,price FROM marks WHERE account_id=?",(aid,))}
    def pending_order(self,aid,symbol):
        with self._c() as c:
            r=c.execute("SELECT * FROM orders WHERE account_id=? AND symbol=? AND status='PENDING' ORDER BY created_at DESC LIMIT 1",(aid,symbol.upper())).fetchone(); return dict(r) if r else None
    def add_order(self,o):
        x=dict(o); x.setdefault("order_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso()); x.setdefault("status","PENDING")
        with self._c() as c:
            c.execute("""INSERT INTO orders(order_id,account_id,symbol,side,created_bar,status,requested_notional,qty,reason,decision_id,created_at)
            VALUES(:order_id,:account_id,:symbol,:side,:created_bar,:status,:requested_notional,:qty,:reason,:decision_id,:created_at)""",x)
        return x["order_id"]
    def fill_order(self,oid,bar,price,fees,slippage):
        with self._c() as c: c.execute("UPDATE orders SET status='FILLED',filled_bar=?,fill_price=?,fees=?,slippage_cost=? WHERE order_id=?",(bar,float(price),float(fees),float(slippage),oid))
    def cancel_order(self,oid,reason=None):
        with self._c() as c:
            if reason is None:
                c.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=? AND status='PENDING'",(oid,))
            else:
                c.execute("UPDATE orders SET status='CANCELLED',reason=? WHERE order_id=? AND status='PENDING'",(str(reason),oid))
    def fill_buy_atomic(self, aid, oid, bar, price, fees, slippage, new_cash, position):
        p=dict(position)
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            cur=c.execute("UPDATE orders SET status='FILLED',filled_bar=?,fill_price=?,fees=?,slippage_cost=? WHERE order_id=? AND status='PENDING'",
                          (bar,float(price),float(fees),float(slippage),oid))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(new_cash),aid))
            c.execute("""INSERT INTO positions(account_id,symbol,qty,avg_entry,entry_bar,strategy,horizon,regime_entry,stop_price,target_price,max_holding_bars,bars_held,leverage_at_entry)
            VALUES(:account_id,:symbol,:qty,:avg_entry,:entry_bar,:strategy,:horizon,:regime_entry,:stop_price,:target_price,:max_holding_bars,:bars_held,:leverage_at_entry)
            ON CONFLICT(account_id,symbol) DO UPDATE SET qty=excluded.qty,avg_entry=excluded.avg_entry,entry_bar=excluded.entry_bar,
            strategy=excluded.strategy,horizon=excluded.horizon,regime_entry=excluded.regime_entry,stop_price=excluded.stop_price,target_price=excluded.target_price,
            max_holding_bars=excluded.max_holding_bars,bars_held=excluded.bars_held,leverage_at_entry=excluded.leverage_at_entry""",p)
            c.execute("UPDATE positions SET entry_order_id=? WHERE account_id=? AND symbol=?",
                      (oid, aid, p["symbol"]))
        self._checkpoint_persistent()
        return True

    def fill_sell_atomic(self, aid, oid, bar, price, fees, slippage, new_cash, trade, symbol):
        x=dict(trade); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso()); x["exit_order_id"]=oid
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            cur=c.execute("UPDATE orders SET status='FILLED',filled_bar=?,fill_price=?,fees=?,slippage_cost=? WHERE order_id=? AND status='PENDING'",
                          (bar,float(price),float(fees),float(slippage),oid))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(new_cash),aid))
            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,exit_order_id,created_at)
            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:exit_order_id,:created_at)""",x)
            c.execute("UPDATE trades SET entry_order_id=(SELECT entry_order_id FROM positions WHERE account_id=? AND symbol=?) WHERE trade_id=?",
                      (aid, str(symbol).upper(), x["trade_id"]))
            c.execute("DELETE FROM positions WHERE account_id=? AND symbol=?",(aid,str(symbol).upper()))
        self._checkpoint_persistent()
        return True

    def close_position_atomic(self, aid, new_cash, trade, symbol):
        x=dict(trade); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso()); x.setdefault("exit_order_id",None)
        with self._c() as c:
            c.execute("BEGIN IMMEDIATE")
            exists=c.execute("SELECT 1 FROM positions WHERE account_id=? AND symbol=?",(aid,str(symbol).upper())).fetchone()
            if not exists:
                return False
            c.execute("UPDATE accounts SET cash=? WHERE account_id=?",(float(new_cash),aid))
            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,exit_order_id,created_at)
            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:exit_order_id,:created_at)""",x)
            c.execute("UPDATE trades SET entry_order_id=(SELECT entry_order_id FROM positions WHERE account_id=? AND symbol=?) WHERE trade_id=?",
                      (aid, str(symbol).upper(), x["trade_id"]))
            c.execute("DELETE FROM positions WHERE account_id=? AND symbol=?",(aid,str(symbol).upper()))
        self._checkpoint_persistent()
        return True

    def add_trade(self,t):
        x=dict(t); x.setdefault("trade_id",uuid.uuid4().hex); x.setdefault("created_at",now_iso()); x.setdefault("exit_order_id",None)
        with self._c() as c:
            c.execute("""INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,exit_order_id,created_at)
            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:exit_order_id,:created_at)""",x)
    def save_equity(self,aid,bar,equity,cash,gross,lev,dd):
        with self._c() as c: c.execute("INSERT INTO equity_history(account_id,bar_time,equity,cash,gross_exposure,leverage,drawdown) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,bar_time) DO UPDATE SET equity=excluded.equity,cash=excluded.cash,gross_exposure=excluded.gross_exposure,leverage=excluded.leverage,drawdown=excluded.drawdown",(aid,bar,float(equity),float(cash),float(gross),float(lev),float(dd)))
    def peak_equity(self,aid):
        with self._c() as c:
            r=c.execute("SELECT MAX(equity) FROM equity_history WHERE account_id=?",(aid,)).fetchone(); return float(r[0]) if r and r[0] is not None else None
    def add_diagnostic(self,aid,symbol,horizon,bar,category,detail,payload=None):
        with self._c() as c: c.execute("INSERT INTO diagnostics(account_id,symbol,horizon,bar_time,category,detail,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",(aid,symbol.upper(),horizon,bar,category,detail,json.dumps(payload or {},ensure_ascii=False),now_iso()))
    def recent_decisions(self,limit=200):
        with self._c() as c:return [dict(x) for x in c.execute("SELECT * FROM decisions ORDER BY bar_time DESC LIMIT ?",(int(limit),))]
    def recent_trades(self,limit=200):
        with self._c() as c:return [dict(x) for x in c.execute("SELECT * FROM trades ORDER BY exit_bar DESC LIMIT ?",(int(limit),))]
    def equity(self,aid,limit=5000):
        with self._c() as c:return [dict(x) for x in c.execute("SELECT * FROM equity_history WHERE account_id=? ORDER BY bar_time LIMIT ?",(aid,int(limit)))]
    def diagnostics(self,limit=200):
        with self._c() as c:return [dict(x) for x in c.execute("SELECT * FROM diagnostics ORDER BY id DESC LIMIT ?",(int(limit),))]
