from __future__ import annotations
import yaml
from dotenv import load_dotenv
from src.backtest import ExecutionCosts, RiskRules
from src.forward import ForwardManager, ForwardConfig, rank_forward
from src.forward_db import ForwardDB

load_dotenv()
with open("config.yaml","r",encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

db = ForwardDB("forward_validation.sqlite3")
fc = ForwardConfig(
    stock_costs=ExecutionCosts(**cfg["execution"]["stock"]),
    crypto_costs=ExecutionCosts(**cfg["execution"]["crypto"]),
    risk=RiskRules(
        max_position_pct=cfg["risk"]["max_position_pct"],
        stop_loss_pct=cfg["risk"]["stop_loss_pct"],
        take_profit_pct=cfg["risk"]["take_profit_pct"],
    ),
    stock_bars_per_year=cfg["research"]["bars_per_year_stock_daily"],
    crypto_bars_per_year=cfg["research"]["bars_per_year_crypto_daily"],
)
manager = ForwardManager(db, fc)
print(manager.run_once())
ranking = rank_forward(db, fc.stock_bars_per_year, fc.crypto_bars_per_year)
if len(ranking):
    print(ranking.to_string(index=False))
else:
    print("No forward candidates registered yet.")
