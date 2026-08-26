"""Crypto V2 shadow research engine.

This package is intentionally isolated from the production SimulationDB ledger.
It reads the shared market cache but writes only to crypto_v2_shadow.sqlite3.
"""

from .core import classify_market_regime, symbol_features, route_strategy
from .shadow_db import CryptoV2ShadowDB
from .shadow_engine import CryptoV2ShadowEngine

__all__ = [
    "classify_market_regime",
    "symbol_features",
    "route_strategy",
    "CryptoV2ShadowDB",
    "CryptoV2ShadowEngine",
]
