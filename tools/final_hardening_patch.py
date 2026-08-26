from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# --- ExecutionCosts: add asymmetric sell tax while preserving one_way_rate compatibility.
replace(
    "src/backtest.py",
    '''@dataclass(frozen=True)\nclass ExecutionCosts:\n    commission_bps: float = 0.0\n    slippage_bps: float = 0.0\n    spread_bps: float = 0.0\n\n    @property\n    def one_way_rate(self) -> float:\n        # Conservative model: half-spread + slippage + commission.\n        return (self.commission_bps + self.slippage_bps + self.spread_bps / 2.0) / 10000.0\n''',
    '''@dataclass(frozen=True)\nclass ExecutionCosts:\n    commission_bps: float = 0.0\n    slippage_bps: float = 0.0\n    spread_bps: float = 0.0\n    sell_tax_bps: float = 0.0\n\n    @property\n    def buy_rate(self) -> float:\n        return (self.commission_bps + self.slippage_bps + self.spread_bps / 2.0) / 10000.0\n\n    @property\n    def sell_rate(self) -> float:\n        return (self.commission_bps + self.slippage_bps + self.spread_bps / 2.0 + self.sell_tax_bps) / 10000.0\n\n    @property\n    def one_way_rate(self) -> float:\n        # Compatibility value for callers that still require a symmetric rate.\n        return (self.buy_rate + self.sell_rate) / 2.0\n''',
)
replace("src/backtest.py", "    cost_rate = costs.one_way_rate\n", "    buy_cost_rate = costs.buy_rate\n    sell_cost_rate = costs.sell_rate\n")
replace("src/backtest.py", "            fill = o * (1 + cost_rate)\n", "            fill = o * (1 + buy_cost_rate)\n")
replace("src/backtest.py", "            fill = o * (1 - cost_rate)\n", "            fill = o * (1 - sell_cost_rate)\n")
replace("src/backtest.py", "                exit_price, reason = stop * (1 - cost_rate), \"STOP\"\n", "                exit_price, reason = stop * (1 - sell_cost_rate), \"STOP\"\n")
replace("src/backtest.py", "                exit_price, reason = target * (1 - cost_rate), \"TAKE_PROFIT\"\n", "                exit_price, reason = target * (1 - sell_cost_rate), \"TAKE_PROFIT\"\n")
replace("src/backtest.py", "        fill = float(data[\"close\"].iloc[-1]) * (1 - cost_rate)\n", "        fill = float(data[\"close\"].iloc[-1]) * (1 - sell_cost_rate)\n")


# --- Simulation engine: explicit buy/sell cost hooks.
replace(
    "src/simulation_engine.py",
    '''    def _cost_rate(self,market):\n        c=ExecutionCosts(0,3,2) if market==\"stock\" else ExecutionCosts(10,5,4)\n        return c.one_way_rate\n''',
    '''    def _cost_rate(self,market):\n        c=ExecutionCosts(0,3,2) if market==\"stock\" else ExecutionCosts(10,5,4)\n        return c.one_way_rate\n\n    def _buy_cost_rate(self, market):\n        return self._cost_rate(market)\n\n    def _sell_cost_rate(self, market):\n        return self._cost_rate(market)\n''',
)
replace("src/simulation_engine.py", "        acct=self.db.account(aid); pos=self.db.position(aid,symbol); rate=self._cost_rate(market)\n", "        acct=self.db.account(aid); pos=self.db.position(aid,symbol)\n")
replace("src/simulation_engine.py", "            fill=open_px*(1+rate); qty=notional/fill; fees=notional*rate\n", "            rate=self._buy_cost_rate(market)\n            fill=open_px*(1+rate); qty=notional/fill; fees=notional*rate\n")
replace("src/simulation_engine.py", "        if o[\"side\"]==\"SELL\" and pos is not None:\n            fill=open_px*(1-rate);", "        if o[\"side\"]==\"SELL\" and pos is not None:\n            rate=self._sell_cost_rate(market)\n            fill=open_px*(1-rate);")
replace("src/simulation_engine.py", "        rate=self._cost_rate(market); fill=exit_px*(1-rate); acct=self.db.account(aid)\n", "        rate=self._sell_cost_rate(market); fill=exit_px*(1-rate); acct=self.db.account(aid)\n")
replace("src/simulation_engine.py", "        rate=self._cost_rate(market)\n        marks=self.db.marks(aid)\n", "        rate=self._sell_cost_rate(market)\n        marks=self.db.marks(aid)\n")


# --- Taiwan: commission applies both sides; 0.3% stock transaction tax applies only on sell.
replace(
    "src/twstock_support.py",
    '''def _tw_costs() -> ExecutionCosts:\n    # Conservative symmetric approximation: nominal broker commission plus half of\n    # the stock transaction tax distributed across both sides, then slippage/spread.\n    return ExecutionCosts(commission_bps=29.25, slippage_bps=5.0, spread_bps=4.0)\n''',
    '''def _tw_costs() -> ExecutionCosts:\n    # Taiwan cash-stock model: broker commission on both sides and the regular\n    # 0.3% securities transaction tax only on the sell side. Day-trade tax relief\n    # is intentionally not assumed because this simulator is not day-trade-only.\n    return ExecutionCosts(commission_bps=14.25, slippage_bps=5.0, spread_bps=4.0, sell_tax_bps=30.0)\n''',
)
replace(
    "src/twstock_support.py",
    '''    def _cost_rate(self, market):\n        if market == TW_MARKET:\n            return _tw_costs().one_way_rate\n        return super()._cost_rate(market)\n''',
    '''    def _cost_rate(self, market):\n        if market == TW_MARKET:\n            return _tw_costs().one_way_rate\n        return super()._cost_rate(market)\n\n    def _buy_cost_rate(self, market):\n        if market == TW_MARKET:\n            return _tw_costs().buy_rate\n        return super()._buy_cost_rate(market)\n\n    def _sell_cost_rate(self, market):\n        if market == TW_MARKET:\n            return _tw_costs().sell_rate\n        return super()._sell_cost_rate(market)\n''',
)
replace("src/twstock_support.py", "        pos = self.db.position(aid, symbol)\n        rate = self._cost_rate(market)\n        open_px = float(row.open)\n", "        pos = self.db.position(aid, symbol)\n        open_px = float(row.open)\n")
replace("src/twstock_support.py", "            fill = open_px * (1 + rate)\n", "            rate = self._buy_cost_rate(market)\n            fill = open_px * (1 + rate)\n")
replace("src/twstock_support.py", "        if o[\"side\"] == \"SELL\" and pos is not None:\n            fill = open_px * (1 - rate)\n", "        if o[\"side\"] == \"SELL\" and pos is not None:\n            rate = self._sell_cost_rate(market)\n            fill = open_px * (1 - rate)\n")


# --- SimulationDB: versioned migration, exit-order uniqueness and immediate persistent checkpoint.
replace("src/simulation_db.py", "import json, sqlite3, uuid\n", "import json, os, sqlite3, uuid\nfrom pathlib import Path\n")
replace("src/simulation_db.py", "def now_iso(): return datetime.now(timezone.utc).isoformat()\n\n\nclass SimulationDB:", "def now_iso(): return datetime.now(timezone.utc).isoformat()\n\nSCHEMA_VERSION = 1\n\n\nclass SimulationDB:")
replace(
    "src/simulation_db.py",
    '''              return_pct REAL NOT NULL, strategy TEXT, horizon TEXT, regime_entry TEXT,\n              exit_reason TEXT, leverage REAL, created_at TEXT NOT NULL);\n''',
    '''              return_pct REAL NOT NULL, strategy TEXT, horizon TEXT, regime_entry TEXT,\n              exit_reason TEXT, leverage REAL, exit_order_id TEXT, created_at TEXT NOT NULL);\n''',
)
replace(
    "src/simulation_db.py",
    '''            \"\"\")\n\n    def ensure_accounts(self, initial_equity: float = 100000.0):\n''',
    '''            \"\"\")\n            self._migrate(c)\n\n    def _migrate(self, c):\n        current = int(c.execute(\"PRAGMA user_version\").fetchone()[0])\n        if current < 1:\n            cols = {str(r[1]) for r in c.execute(\"PRAGMA table_info(trades)\").fetchall()}\n            if \"exit_order_id\" not in cols:\n                c.execute(\"ALTER TABLE trades ADD COLUMN exit_order_id TEXT\")\n            c.execute(\"CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_exit_order_id ON trades(exit_order_id) WHERE exit_order_id IS NOT NULL\")\n            c.execute(\"PRAGMA user_version=1\")\n        else:\n            c.execute(\"CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_exit_order_id ON trades(exit_order_id) WHERE exit_order_id IS NOT NULL\")\n        if current > SCHEMA_VERSION:\n            raise RuntimeError(f\"simulation DB schema {current} is newer than supported {SCHEMA_VERSION}\")\n\n    def _checkpoint_persistent(self):\n        persist_root = os.getenv(\"V6_PERSISTENT_DATA_DIR\")\n        if not persist_root:\n            return False\n        src = Path(self.path)\n        if src.name != \"simulation_lab.sqlite3\" or not src.exists():\n            return False\n        current = Path(persist_root) / \"v6-snapshots\" / \"current\"\n        current.mkdir(parents=True, exist_ok=True)\n        target = current / src.name\n        tmp = current / f\".{src.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp\"\n        src_con = dst_con = None\n        try:\n            src_con = sqlite3.connect(str(src), timeout=30)\n            dst_con = sqlite3.connect(str(tmp), timeout=30)\n            src_con.backup(dst_con, pages=256, sleep=0.01)\n            dst_con.commit()\n            row = dst_con.execute(\"PRAGMA quick_check\").fetchone()\n            if not row or str(row[0]).lower() != \"ok\":\n                raise sqlite3.DatabaseError(\"checkpoint quick_check failed\")\n            dst_con.close(); dst_con = None\n            src_con.close(); src_con = None\n            os.replace(tmp, target)\n            return True\n        except Exception:\n            return False\n        finally:\n            if dst_con is not None:\n                dst_con.close()\n            if src_con is not None:\n                src_con.close()\n            try:\n                tmp.unlink(missing_ok=True)\n            except Exception:\n                pass\n\n    def ensure_accounts(self, initial_equity: float = 100000.0):\n''',
)
replace("src/simulation_db.py", "        return True\n\n    def fill_sell_atomic", "        self._checkpoint_persistent()\n        return True\n\n    def fill_sell_atomic",)
replace("src/simulation_db.py", "        x=dict(trade); x.setdefault(\"trade_id\",uuid.uuid4().hex); x.setdefault(\"created_at\",now_iso())\n        with self._c() as c:\n            c.execute(\"BEGIN IMMEDIATE\")\n            cur=c.execute(\"UPDATE orders SET status='FILLED'", "        x=dict(trade); x.setdefault(\"trade_id\",uuid.uuid4().hex); x.setdefault(\"created_at\",now_iso()); x[\"exit_order_id\"]=oid\n        with self._c() as c:\n            c.execute(\"BEGIN IMMEDIATE\")\n            cur=c.execute(\"UPDATE orders SET status='FILLED'",)
replace(
    "src/simulation_db.py",
    '''            c.execute(\"\"\"INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)\"\"\",x)\n            c.execute(\"DELETE FROM positions WHERE account_id=? AND symbol=?\",(aid,str(symbol).upper()))\n        return True\n\n    def close_position_atomic''',
    '''            c.execute(\"\"\"INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,exit_order_id,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:exit_order_id,:created_at)\"\"\",x)\n            c.execute(\"DELETE FROM positions WHERE account_id=? AND symbol=?\",(aid,str(symbol).upper()))\n        self._checkpoint_persistent()\n        return True\n\n    def close_position_atomic''',
)
replace("src/simulation_db.py", "        x=dict(trade); x.setdefault(\"trade_id\",uuid.uuid4().hex); x.setdefault(\"created_at\",now_iso())\n        with self._c() as c:\n            c.execute(\"BEGIN IMMEDIATE\")\n            exists=c.execute", "        x=dict(trade); x.setdefault(\"trade_id\",uuid.uuid4().hex); x.setdefault(\"created_at\",now_iso()); x.setdefault(\"exit_order_id\",None)\n        with self._c() as c:\n            c.execute(\"BEGIN IMMEDIATE\")\n            exists=c.execute",)
replace(
    "src/simulation_db.py",
    '''            c.execute(\"\"\"INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)\"\"\",x)\n            c.execute(\"DELETE FROM positions WHERE account_id=? AND symbol=?\",(aid,str(symbol).upper()))\n        return True\n\n    def add_trade(self,t):\n        x=dict(t); x.setdefault(\"trade_id\",uuid.uuid4().hex); x.setdefault(\"created_at\",now_iso())\n''',
    '''            c.execute(\"\"\"INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,exit_order_id,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:exit_order_id,:created_at)\"\"\",x)\n            c.execute(\"DELETE FROM positions WHERE account_id=? AND symbol=?\",(aid,str(symbol).upper()))\n        self._checkpoint_persistent()\n        return True\n\n    def add_trade(self,t):\n        x=dict(t); x.setdefault(\"trade_id\",uuid.uuid4().hex); x.setdefault(\"created_at\",now_iso()); x.setdefault(\"exit_order_id\",None)\n''',
)
replace(
    "src/simulation_db.py",
    '''            c.execute(\"\"\"INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:created_at)\"\"\",x)\n''',
    '''            c.execute(\"\"\"INSERT INTO trades(trade_id,account_id,symbol,entry_bar,exit_bar,qty,entry_price,exit_price,realized_pnl,return_pct,strategy,horizon,regime_entry,exit_reason,leverage,exit_order_id,created_at)\n            VALUES(:trade_id,:account_id,:symbol,:entry_bar,:exit_bar,:qty,:entry_price,:exit_price,:realized_pnl,:return_pct,:strategy,:horizon,:regime_entry,:exit_reason,:leverage,:exit_order_id,:created_at)\"\"\",x)\n''',
)


# --- Regression tests.
Path("tests/test_final_hardening.py").write_text(r'''import os
import sqlite3

from src.backtest import ExecutionCosts
from src.simulation_db import SCHEMA_VERSION, SimulationDB
from src.twstock_support import _tw_costs


def _trade(aid="stock_short", symbol="TEST"):
    return {
        "account_id": aid, "symbol": symbol,
        "entry_bar": "2026-01-01T00:00:00+00:00", "exit_bar": "2026-01-02T00:00:00+00:00",
        "qty": 10.0, "entry_price": 100.0, "exit_price": 110.0,
        "realized_pnl": 100.0, "return_pct": 0.1, "strategy": "trend_ma",
        "horizon": "short", "regime_entry": "bull", "exit_reason": "TEST", "leverage": 1.0,
    }


def _position(aid="stock_short", symbol="TEST"):
    return {
        "account_id": aid, "symbol": symbol, "qty": 10.0, "avg_entry": 100.0,
        "entry_bar": "2026-01-01T00:00:00+00:00", "strategy": "trend_ma", "horizon": "short",
        "regime_entry": "bull", "stop_price": 90.0, "target_price": 130.0,
        "max_holding_bars": 100, "bars_held": 0, "leverage_at_entry": 1.0,
    }


def test_schema_migrates_legacy_trades_table(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades(trade_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL, entry_bar TEXT NOT NULL, exit_bar TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL, realized_pnl REAL NOT NULL, return_pct REAL NOT NULL, strategy TEXT, horizon TEXT, regime_entry TEXT, exit_reason TEXT, leverage REAL, created_at TEXT NOT NULL)")
    con.commit(); con.close()
    db = SimulationDB(str(path))
    with db._c() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        version = c.execute("PRAGMA user_version").fetchone()[0]
    assert "exit_order_id" in cols
    assert version == SCHEMA_VERSION


def test_sell_trade_records_unique_exit_order_id(tmp_path):
    db = SimulationDB(str(tmp_path / "sim.sqlite3")); db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00", "requested_notional": 0, "qty": 10, "reason": "TEST", "decision_id": None})
    assert db.fill_sell_atomic(aid, oid, "2026-01-02T00:00:00+00:00", 110, 0, 0, 101100, _trade(aid, symbol), symbol)
    row = db.recent_trades(1)[0]
    assert row["exit_order_id"] == oid


def test_taiwan_cost_is_asymmetric():
    c = _tw_costs()
    assert isinstance(c, ExecutionCosts)
    assert round(c.sell_rate - c.buy_rate, 8) == 0.003
    assert c.buy_rate < c.one_way_rate < c.sell_rate


def test_trade_checkpoint_writes_persistent_snapshot(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    persistent = tmp_path / "persistent"; persistent.mkdir()
    monkeypatch.setenv("V6_PERSISTENT_DATA_DIR", str(persistent))
    path = runtime / "simulation_lab.sqlite3"
    db = SimulationDB(str(path)); db.ensure_accounts(100000)
    aid, symbol = "stock_short", "TEST"
    db.upsert_position(_position(aid, symbol))
    oid = db.add_order({"account_id": aid, "symbol": symbol, "side": "SELL", "created_bar": "2026-01-02T00:00:00+00:00", "requested_notional": 0, "qty": 10, "reason": "TEST", "decision_id": None})
    assert db.fill_sell_atomic(aid, oid, "2026-01-02T00:00:00+00:00", 110, 0, 0, 101100, _trade(aid, symbol), symbol)
    snap = persistent / "v6-snapshots" / "current" / "simulation_lab.sqlite3"
    assert snap.exists() and snap.stat().st_size > 0
    con = sqlite3.connect(snap)
    assert con.execute("PRAGMA quick_check").fetchone()[0].lower() == "ok"
    assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    con.close()
''', encoding="utf-8")

print("final hardening patch applied")
