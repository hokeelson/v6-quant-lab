import os
import pytest
from src.paper import AlpacaPaperBroker

def test_paper_broker_rejects_live_endpoint(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL","https://api.alpaca.markets")
    monkeypatch.setenv("ALPACA_API_KEY","x")
    monkeypatch.setenv("ALPACA_API_SECRET","y")
    with pytest.raises(RuntimeError):
        AlpacaPaperBroker()

def test_paper_orders_disabled_by_default(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL","https://paper-api.alpaca.markets")
    monkeypatch.setenv("ALPACA_API_KEY","x")
    monkeypatch.setenv("ALPACA_API_SECRET","y")
    monkeypatch.delenv("V6_ALLOW_PAPER_ORDERS",raising=False)
    b=AlpacaPaperBroker()
    assert b.allow is False
    with pytest.raises(RuntimeError):
        b.submit_market_notional_buy("AAPL",100,"cid")
