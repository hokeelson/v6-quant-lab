from pathlib import Path

path = Path('src/simulation_engine.py')
text = path.read_text(encoding='utf-8')
old = '''        acct=self.db.account(aid); pos=self.db.position(aid,symbol); rate=self._cost_rate(market)\n        open_px=float(row.open)\n        if o["side"]=="BUY" and pos is None:\n'''
new = '''        acct=self.db.account(aid); pos=self.db.position(aid,symbol); rate=self._cost_rate(market)\n        open_px=float(row.open)\n        hz=decision_context.get("horizon",aid.split("_",1)[1])\n        if o["side"]=="BUY" and pos is not None:\n            self.db.cancel_order(o["order_id"], "STALE_BUY_POSITION_EXISTS")\n            self.db.add_diagnostic(aid,symbol,hz,ts.isoformat(),"ORDER_CANCELLED","Stale BUY cancelled because position already exists",{\n                "cancel_reason":"STALE_BUY_POSITION_EXISTS","broker_order_api_calls":0,\n            })\n            return "CANCELLED"\n        if o["side"]=="SELL" and pos is None:\n            self.db.cancel_order(o["order_id"], "STALE_SELL_NO_POSITION")\n            self.db.add_diagnostic(aid,symbol,hz,ts.isoformat(),"ORDER_CANCELLED","Stale SELL cancelled because position no longer exists",{\n                "cancel_reason":"STALE_SELL_NO_POSITION","broker_order_api_calls":0,\n            })\n            return "CANCELLED"\n        if o["side"]=="BUY" and pos is None:\n'''
if old not in text:
    raise SystemExit('order-state cleanup anchor not found')
text = text.replace(old, new, 1)
text = text.replace('            hz=decision_context.get("horizon",aid.split("_",1)[1])\n            maxlev=HORIZON_SPECS[hz]["max_leverage"]', '            maxlev=HORIZON_SPECS[hz]["max_leverage"]', 1)
compile(text, 'src/simulation_engine.py', 'exec')
path.write_text(text, encoding='utf-8')
print('order state cleanup applied')
