from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from .bidirectional_registration import is_forward_eligible, registration_state
from .bidirectional_shadow import (
    advance_bidirectional_evaluations,
    bidirectional_summary,
    recent_bidirectional_decisions,
    record_bidirectional_decision,
)
from .core import classify_market_regime, symbol_features
from .research import (
    manage_blocked_candidate,
    market_context,
    recent_blocked_candidates,
    recent_research_trades,
    research_summary,
    update_position_excursion,
)
from .risk import portfolio_status
from .shadow_engine import (
    BAR_DELTAS,
    CRYPTO_FEE_RATE,
    HORIZONS,
    MAX_EVENTS_PER_CYCLE,
    SNAPSHOT_PATH,
    CryptoV2ShadowEngine,
    _iso,
    _through,
    _utc,
)


class ResearchCryptoV2ShadowEngine(CryptoV2ShadowEngine):
    """Research-only overlays that leave V2 routing and execution unchanged."""

    def _manage_position(self, symbol: str, horizon: str, bar_time: str, row) -> str | None:
        # Counterfactuals never touch cash/positions; they only measure what a
        # portfolio-risk-blocked signal would have done.
        manage_blocked_candidate(self.db, symbol, horizon, bar_time, row, CRYPTO_FEE_RATE)
        if self.db.position(symbol, horizon):
            update_position_excursion(self.db, symbol, horizon, float(row.high), float(row.low))
        return super()._manage_position(symbol, horizon, bar_time, row)

    def _process_bar(self, symbol: str, horizon: str, hist: pd.DataFrame, btc_1h: pd.DataFrame, ts) -> dict:
        """Add a three-way LONG/SHORT/NO_TRADE shadow before normal V2 processing.

        The shadow uses only information available on the current closed bar. Its
        evaluation enters on the next bar open and writes only research tables.
        Parent V2 routing, orders, positions, cash and portfolio risk are untouched.
        """
        bar_time = _iso(ts)
        row = hist.loc[ts]

        # Mature older forward evaluations first so the current bar can never be
        # used to score a decision that was not yet known.
        advance_bidirectional_evaluations(
            self.db, symbol, horizon, bar_time, row, CRYPTO_FEE_RATE
        )

        bar_end = _utc(ts) + BAR_DELTAS[horizon]
        btc_cut = _through(btc_1h, bar_end)
        regime = classify_market_regime(btc_cut)
        features = symbol_features(hist, btc_cut)
        context = dict(getattr(self.db, "_research_context", {}) or {})

        # A newly deployed research model must not turn catch-up bars into fake
        # historical decisions. The next executable open (bar_end) must occur at
        # or after this shadow's persisted registration time.
        if is_forward_eligible(self.db, bar_end):
            shadow = record_bidirectional_decision(
                self.db,
                symbol,
                horizon,
                bar_time,
                regime,
                features,
                context,
                CRYPTO_FEE_RATE,
            )
        else:
            shadow = {
                "selected_action": "SKIPPED_PRE_REGISTRATION",
                "long_ev_proxy": None,
                "short_ev_proxy": None,
            }

        out = super()._process_bar(symbol, horizon, hist, btc_1h, ts)
        out["bidirectional_shadow_action"] = shadow.get("selected_action")
        out["bidirectional_long_ev_proxy"] = shadow.get("long_ev_proxy")
        out["bidirectional_short_ev_proxy"] = shadow.get("short_ev_proxy")
        return out

    def cycle(self, now=None) -> dict:
        now = _utc(now or pd.Timestamp.now(tz="UTC"))
        registration = registration_state(self.db)
        btc = self._btc_1h(now)
        symbols = self._symbols()
        processed = []

        events, frames, errors = self._build_event_plan(symbols, now)
        selected = self._bounded_events(events)
        context_cache: dict[tuple[int, str], dict] = {}

        for ts, symbol, horizon in selected:
            try:
                key = (_utc(ts).value, horizon)
                context = context_cache.get(key)
                if context is None:
                    context = market_context(ts, horizon, frames, btc)
                    context_cache[key] = context
                if hasattr(self.db, "set_research_context"):
                    self.db.set_research_context(context)
                closed = frames[(symbol, horizon)]
                hist = _through(closed, ts)
                processed.append(self._process_bar(symbol, horizon, hist, btc, ts))
            except Exception as exc:
                errors.append({
                    "symbol": symbol,
                    "horizon": horizon,
                    "bar_time": _iso(ts),
                    "error": f"{type(exc).__name__}: {exc}",
                })

        if hasattr(self.db, "set_research_context"):
            self.db.set_research_context({})

        portfolio_risk = {
            horizon: portfolio_status(
                self.db.initial_equity,
                self.db.portfolio_state(horizon),
                horizon,
            )
            for horizon in HORIZONS
        }
        remaining = max(0, len(events) - len(selected))
        catchup = {
            "pending_events_at_cycle_start": len(events),
            "processed_events": len(processed),
            "selected_events": len(selected),
            "remaining_events_estimate": remaining,
            "cycle_event_limit": MAX_EVENTS_PER_CYCLE,
            "is_catching_up": remaining > 0,
            "oldest_selected_bar": _iso(selected[0][0]) if selected else None,
            "newest_selected_bar": _iso(selected[-1][0]) if selected else None,
        }
        research = research_summary(self.db)
        research["bidirectional_decision_shadow"] = bidirectional_summary(self.db)
        research["bidirectional_registration"] = registration
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
            "catchup": catchup,
            "errors": errors,
            "latest_market_regime": self._latest_market_regime(btc),
            "portfolio_risk": portfolio_risk,
            "v2": self.db.summary(),
            "baseline": self._baseline_summary(),
            "positions": self.db.positions(),
            "recent_trades": self.db.recent_trades(100),
            "recent_decisions": self.db.recent_decisions(200),
            "research_layer": research,
            "recent_research_trades": recent_research_trades(self.db, 100),
            "recent_blocked_candidates": recent_blocked_candidates(self.db, 100),
            "recent_bidirectional_decisions": recent_bidirectional_decisions(self.db, 100),
        }
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        tmp.replace(SNAPSHOT_PATH)
        return payload

    @staticmethod
    def _latest_market_regime(btc):
        # Reuse the exact parent classifier without changing any thresholds.
        return classify_market_regime(btc)
