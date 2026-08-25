from pathlib import Path

DB_PATH = Path("src/simulation_db.py")
ENG_PATH = Path("src/simulation_engine.py")


def patch_db() -> bool:
    text = DB_PATH.read_text(encoding="utf-8")
    marker = """    def fill_order(self,oid,bar,price,fees,slippage):\n        with self._c() as c: c.execute(\"UPDATE orders SET status='FILLED',filled_bar=?,fill_price=?,fees=?,slippage_cost=? WHERE order_id=?\",(bar,float(price),float(fees),float(slippage),oid))\n"""
    addition = marker + """    def cancel_order(self,oid,reason=None):\n        with self._c() as c:\n            if reason is None:\n                c.execute(\"UPDATE orders SET status='CANCELLED' WHERE order_id=? AND status='PENDING'\",(oid,))\n            else:\n                c.execute(\"UPDATE orders SET status='CANCELLED',reason=? WHERE order_id=? AND status='PENDING'\",(str(reason),oid))\n"""
    if "def cancel_order(self,oid,reason=None):" in text:
        return False
    if marker not in text:
        raise RuntimeError("simulation_db.py fill_order anchor not found")
    DB_PATH.write_text(text.replace(marker, addition, 1), encoding="utf-8")
    return True


def patch_engine() -> bool:
    text = ENG_PATH.read_text(encoding="utf-8")
    old = """            notional=min(risk_adjusted,room)\n            if notional<=0:\n                return None\n            fill=open_px*(1+rate); qty=notional/fill; fees=notional*rate\n"""
    new = """            notional=min(risk_adjusted,room)\n            if notional<=0:\n                cancel_reason = \"RISK_SIZING_ZERO_NOTIONAL\" if risk_adjusted <= 0 else \"NO_LEVERAGE_ROOM\"\n                self.db.cancel_order(o[\"order_id\"], cancel_reason)\n                self.db.add_diagnostic(aid,symbol,hz,ts.isoformat(),\"ORDER_CANCELLED\",\"Pending BUY cancelled before fill\",{\n                    **sizing,\"leverage_room\":room,\"risk_adjusted_notional\":risk_adjusted,\n                    \"requested_notional\":original_notional,\"cancel_reason\":cancel_reason,\n                    \"broker_order_api_calls\":0,\n                })\n                return \"CANCELLED\"\n            fill=open_px*(1+rate); qty=notional/fill; fees=notional*rate\n"""
    if 'return "CANCELLED"' in text and "RISK_SIZING_ZERO_NOTIONAL" in text:
        return False
    if old not in text:
        raise RuntimeError("simulation_engine.py zero-notional anchor not found")
    ENG_PATH.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def self_check():
    db = DB_PATH.read_text(encoding="utf-8")
    eng = ENG_PATH.read_text(encoding="utf-8")
    assert "def cancel_order(self,oid,reason=None):" in db
    assert "RISK_SIZING_ZERO_NOTIONAL" in eng
    assert "NO_LEVERAGE_ROOM" in eng
    assert 'return "CANCELLED"' in eng
    compile(db, str(DB_PATH), "exec")
    compile(eng, str(ENG_PATH), "exec")


if __name__ == "__main__":
    changed = patch_db() or patch_engine()
    # patch_engine must still run when patch_db changed; call again safely.
    patch_engine()
    self_check()
    print("pending-order patch OK", changed)
