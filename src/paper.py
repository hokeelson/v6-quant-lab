from __future__ import annotations
import os
from urllib.parse import urlparse
import requests

PAPER_HOST = "paper-api.alpaca.markets"

class AlpacaPaperBroker:
    """
    Paper-only Alpaca adapter.

    Safety invariants:
    - Refuses any hostname other than paper-api.alpaca.markets.
    - Order submission requires V6_ALLOW_PAPER_ORDERS=true.
    - This adapter never points to api.alpaca.markets (live trading).
    """
    def __init__(self):
        self.key = os.getenv("ALPACA_API_KEY")
        self.secret = os.getenv("ALPACA_API_SECRET")
        self.base = os.getenv("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        # Accept the dashboard-style endpoint ending in /v2, but normalize it to the host base.
        if self.base.endswith("/v2"):
            self.base = self.base[:-3]
        self.allow = os.getenv("V6_ALLOW_PAPER_ORDERS", "false").lower() == "true"
        host = urlparse(self.base).hostname
        if host != PAPER_HOST:
            raise RuntimeError(
                f"Refusing non-paper Alpaca endpoint: {self.base}. "
                f"Expected https://{PAPER_HOST}"
            )

    def _headers(self):
        if not self.key or not self.secret:
            raise RuntimeError("Alpaca API keys missing.")
        return {"APCA-API-KEY-ID":self.key, "APCA-API-SECRET-KEY":self.secret}

    def _require_orders_enabled(self):
        if not self.allow:
            raise RuntimeError(
                "Alpaca Paper order submission is disabled. "
                "Set V6_ALLOW_PAPER_ORDERS=true explicitly to enable PAPER orders."
            )

    def account(self):
        r = requests.get(f"{self.base}/v2/account", headers=self._headers(), timeout=30)
        r.raise_for_status(); return r.json()

    def positions(self):
        r = requests.get(f"{self.base}/v2/positions", headers=self._headers(), timeout=30)
        r.raise_for_status(); return r.json()

    def position(self, symbol: str):
        r = requests.get(f"{self.base}/v2/positions/{symbol.upper()}", headers=self._headers(), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status(); return r.json()

    def get_order_by_client_id(self, client_order_id: str):
        r = requests.get(
            f"{self.base}/v2/orders:by_client_order_id", headers=self._headers(),
            params={"client_order_id":client_order_id}, timeout=30,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status(); return r.json()

    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str | None = None):
        self._require_orders_enabled()
        payload = {
            "symbol":symbol.upper(), "qty":str(qty), "side":side.lower(),
            "type":"market", "time_in_force":"day",
        }
        if client_order_id:
            payload["client_order_id"] = client_order_id[:128]
        r = requests.post(f"{self.base}/v2/orders", headers=self._headers(), json=payload, timeout=30)
        r.raise_for_status(); return r.json()

    def submit_market_notional_buy(self, symbol: str, notional: float, client_order_id: str):
        self._require_orders_enabled()
        if notional <= 0:
            raise ValueError("notional must be > 0")
        payload = {
            "symbol":symbol.upper(), "notional":f"{float(notional):.2f}", "side":"buy",
            "type":"market", "time_in_force":"day", "client_order_id":client_order_id[:128],
        }
        r = requests.post(f"{self.base}/v2/orders", headers=self._headers(), json=payload, timeout=30)
        r.raise_for_status(); return r.json()

    def close_position(self, symbol: str):
        self._require_orders_enabled()
        r = requests.delete(f"{self.base}/v2/positions/{symbol.upper()}", headers=self._headers(), timeout=30)
        if r.status_code == 404:
            return {"status":"no_position", "symbol":symbol.upper()}
        r.raise_for_status(); return r.json()
