from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..backtest import ExecutionCosts
from ..market_cache import MarketCache, TIMEFRAME_MAP
from ..paths import data_dir
from .core import classify_market_regime, route_strategy, symbol_features
from .shadow_db import CryptoV2ShadowDB


HORIZONS = ("short", "medium", "long")
BAR_DELTAS = {
    "short": pd.Timedelta(hours=1),
    "medium": pd.Timedelta(hours=4),
    "long": pd.Timedelta(days=1),
}
CRYPTO_FEE_RATE = float(ExecutionCosts(10, 5, 4).one_way_rate)
SNAPSHOT_PATH = Path(data_dir()) / "crypto_v2_shadow_snapshot.json"


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(ts) -> str:
    return _utc(ts).isoformat()


class CryptoV2ShadowEngine:
    """Cache-only Crypto V2 forward shadow simulator.

    It reads symbols and OHLCV from the existing V6 state, but all decisions,
    orders, positions and trades are stored in CryptoV2ShadowDB only.
    """

    def __init__(self, baseline_db, cache: MarketCache, shadow_db: CryptoV2ShadowDB):
        self.baseline_db = baseline_db
        self.cache = cache
        self.db = shadow_db

    def _symbols(self) -> list[str]:
        return sorted({
            str(r.get("symbol") or "").upper()
            for r in self.baseline_db.assets()
            if str(r.get("market") or "") == "crypto" and r.get("symbol")
        })

    def _closed(self, symbol: str, horizon: str, now) -> pd.DataFrame:
        _, tf = TIMEFRAME_MAP[("crypto", horizon)]
        raw = self.cache.get("crypto", symbol, tf)
        return self.cache.closed_only(raw, "crypto", horizon, now)

    def _btc_1h(self, now) -> pd.DataFrame:
        raw = self.cache.get("crypto", "BTCUSDT", "1h")
        return self.cache.closed_only(raw, "crypto", "short", now)

    def _decision_row(self, decision_id: str) -> dict:
        with self.db._c() as c:
            r = c.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            return dict(r) if r else {}

    def _baseline_summary(self) -> dict:
        accounts = []
        total_initial = total_equity = 0.0
        total_closed = 0
        for horizon in HORIZONS:
            aid = f"crypto_{horizon}"
            acct = self.baseline_db.account(aid) or {}
            initial = float(acct.get("initial_equity") or 100000.0)
            cash = float(acct.get("cash") or initial)
            marks = self.baseline_db.marks(aid)
            gross = 0.0
            for p in self.baseline_db.positions(aid):
                px = float(marks.get(str(p.get("symbol") or "").upper(), p.get("avg_entry") or 0.0) or 0.0)
                gross += float(p.get("qty") or 0.0) * px
            equity = cash + gross
            trades = [t for t in self.baseline_db.recent_trades(5000) if str(t.get("account_id")) == aid]
            accounts.append({
                "horizon": horizon,
                "initial_equity": initial,
                "equity": equity,
                "return_pct": equity / initial - 1.0 if initial else None,
                "closed_trades": len(trades),
            })
            total_initial += initial
            total_equity += equity
            total_closed += len(trades)
        return {
            "accounts": accounts,
            "initial_equity": total_initial,
            "equity": total_equity,
            "return_pct": total_equity / total_initial - 1.0 if total_initial else None,
            "closed_trades": total_closed,
        }

    def _manage_position(self, symbol: str, horizon: str, bar_time: str, row) -> str | None:
        p = self.db.position(symbol, horizon)
        if not p:
            return None

        o, h, l, c = map(float, (row.open, row.high, row.low, row.close))
        stop = float(p["stop_price"])
        target = float(p["target_price"])

        # Conservative same-bar precedence: stop before target. Gap-through stops
        # fill at the worse open, matching the production simulator's hardening.
        if l <= stop:
            px = min(o, stop) if o < stop else stop
            self.db.close_position(symbol, horizon, bar_time, px, CRYPTO_FEE_RATE, "STOP")
            return "STOP"
        if h >= target:
            self.db.close_position(symbol, horizon, bar_time, target, CRYPTO_FEE_RATE, "TARGET")
            return "TARGET"

        self.db.increment_holding(symbol, horizon)
        p2 = self.db.position(symbol, horizon) or p
        if int(p2.get("bars_held") or 0) >= int(p2.get("max_holding_bars") or 8):
            self.db.close_position(symbol, horizon, bar_time, c, CRYPTO_FEE_RATE, "TIME")
            return "TIME"
        return None

    def _process_bar(self, symbol: str, horizon: str, hist: pd.DataFrame, btc_1h: pd.DataFrame, ts) -> dict:
        bar_time = _iso(ts)
        row = hist.loc[ts]
        bar_end = _utc(ts) + BAR_DELTAS[horizon]
        btc_cut = btc_1h.loc[btc_1h.index <= bar_end].copy()
        regime = classify_market_regime(btc_cut)
        self.db.add_market_state(_iso(bar_end), regime)

        pending = self.db.pending_order(symbol, horizon)
        if pending and _utc(pending["created_bar"]) < _utc(ts):
            prior = self._decision_row(str(pending.get("decision_id") or ""))
            prior_decision = {
                "strategy": prior.get("strategy") or "UNKNOWN",
                "stop_distance": 0.03,
                "target_distance": 0.06,
                "max_holding_bars": {"short": 8, "medium": 10, "long": 12}[horizon],
            }
            # Recover exact routing parameters from the prior bar deterministically.
            prior_ts = _utc(pending["created_bar"])
            prior_hist = hist.loc[hist.index <= prior_ts]
            prior_btc_end = prior_ts + BAR_DELTAS[horizon]
            prior_btc = btc_1h.loc[btc_1h.index <= prior_btc_end]
            prior_regime = classify_market_regime(prior_btc)
            prior_features = symbol_features(prior_hist, prior_btc)
            prior_decision.update(route_strategy(prior_regime, prior_features, horizon))
            self.db.fill_buy(pending, bar_time, float(row.open), CRYPTO_FEE_RATE, prior_decision, prior_regime)

        exit_reason = self._manage_position(symbol, horizon, bar_time, row)

        features = symbol_features(hist, btc_cut)
        if self.db.position(symbol, horizon):
            decision = {
                "action": "NO_TRADE", "strategy": "NONE", "confidence": 0.0,
                "stop_distance": 0.0, "target_distance": 0.0, "max_holding_bars": 0,
                "reason": "Existing V2 shadow position",
            }
        elif self.db.pending_order(symbol, horizon):
            decision = {
                "action": "NO_TRADE", "strategy": "NONE", "confidence": 0.0,
                "stop_distance": 0.0, "target_distance": 0.0, "max_holding_bars": 0,
                "reason": "Pending V2 shadow entry",
            }
        else:
            decision = route_strategy(regime, features, horizon)

        did = self.db.add_decision(symbol, horizon, bar_time, decision, regime, features)
        if decision.get("action") == "ENTER":
            acct = self.db.account(horizon)
            cash = float(acct.get("cash") or 0.0)
            confidence = float(decision.get("confidence") or 0.0)
            # Cash-only V2: keep individual entries intentionally small while the
            # experiment is young. No V2 position can consume more than 10% of the
            # horizon account before fees.
            requested = min(cash, self.db.initial_equity * 0.10 * max(0.50, confidence))
            if requested > 0:
                self.db.add_buy_order(symbol, horizon, bar_time, requested, did)

        self.db.set_last_processed(symbol, horizon, bar_time)
        return {
            "symbol": symbol,
            "horizon": horizon,
            "bar_time": bar_time,
            "regime": regime.get("state"),
            "action": decision.get("action"),
            "strategy": decision.get("strategy"),
            "exit_reason": exit_reason,
        }

    def cycle(self, now=None) -> dict:
        now = _utc(now or pd.Timestamp.now(tz="UTC"))
        btc = self._btc_1h(now)
        symbols = self._symbols()
        processed = []
        errors = []

        for symbol in symbols:
            for horizon in HORIZONS:
                try:
                    closed = self._closed(symbol, horizon, now)
                    if closed is None or closed.empty:
                        continue
                    last = self.db.last_processed(symbol, horizon)
                    if last is None:
                        # Registration point: only the latest already-closed bar is
                        # eligible. No historical V2 decisions/trades are fabricated.
                        new_index = [closed.index[-1]]
                    else:
                        last_ts = _utc(last)
                        new_index = [ts for ts in closed.index if _utc(ts) > last_ts]
                    for ts in new_index:
                        hist = closed.loc[closed.index <= ts].copy()
                        processed.append(self._process_bar(symbol, horizon, hist, btc, ts))
                except Exception as exc:
                    errors.append({
                        "symbol": symbol, "horizon": horizon,
                        "error": f"{type(exc).__name__}: {exc}",
                    })

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "ONLINE" if not errors else "DEGRADED",
            "scope": "CRYPTO_V2_SHADOW_ONLY",
            "forward_only": True,
            "shared_cache_only": True,
            "market_data_api_calls": 0,
            "broker_order_api_calls": 0,
            "symbols": len(symbols),
            "bars_processed": len(processed),
            "errors": errors,
            "latest_market_regime": classify_market_regime(btc),
            "v2": self.db.summary(),
            "baseline": self._baseline_summary(),
            "positions": self.db.positions(),
            "recent_trades": self.db.recent_trades(100),
            "recent_decisions": self.db.recent_decisions(200),
        }
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        tmp.replace(SNAPSHOT_PATH)
        return payload
