from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class PortfolioEVCandidate:
    symbol: str
    market: str
    horizon: str
    strategy: str
    expected_value_pct: float
    expected_value_r: float
    evidence_weight: float
    confidence: float
    correlation_penalty: float
    exposure_penalty: float
    portfolio_ev_score: float
    rank: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _finite(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _clamp01(value, default=0.0):
    return max(0.0, min(1.0, _finite(value, default)))


def score_portfolio_candidate(candidate: dict) -> PortfolioEVCandidate:
    """Score one candidate for portfolio-level EV ranking.

    The score is intentionally observational. It rewards positive trade EV and
    evidence quality while discounting candidates that would add highly correlated
    or already-concentrated exposure. It does not authorize or place any order.
    """
    ev_pct = _finite(candidate.get("expected_value_pct"), 0.0)
    ev_r = _finite(candidate.get("expected_value_r"), 0.0)
    evidence = _clamp01(candidate.get("evidence_weight"), 0.0)
    confidence = _clamp01(_finite(candidate.get("confidence"), 50.0) / 100.0, 0.5)
    corr = _clamp01(candidate.get("correlation_penalty"), 0.0)
    exposure = _clamp01(candidate.get("exposure_penalty"), 0.0)

    # Keep immature candidates eligible but conservative: 35% prior + 65% evidence.
    evidence_factor = 0.35 + 0.65 * evidence
    confidence_factor = 0.70 + 0.30 * confidence
    diversification_factor = (1.0 - 0.55 * corr) * (1.0 - 0.45 * exposure)

    # EV in R is comparable across stop sizes. A small EV-pct term breaks near ties.
    base = ev_r + 0.25 * ev_pct
    score = base * evidence_factor * confidence_factor * diversification_factor

    return PortfolioEVCandidate(
        symbol=str(candidate.get("symbol") or "").upper(),
        market=str(candidate.get("market") or ""),
        horizon=str(candidate.get("horizon") or ""),
        strategy=str(candidate.get("strategy") or ""),
        expected_value_pct=ev_pct,
        expected_value_r=ev_r,
        evidence_weight=evidence,
        confidence=confidence * 100.0,
        correlation_penalty=corr,
        exposure_penalty=exposure,
        portfolio_ev_score=float(score),
    )


def rank_portfolio_ev(candidates: list[dict], positive_only: bool = True) -> list[dict]:
    """Rank simultaneous candidates by risk-normalized portfolio EV."""
    scored = [score_portfolio_candidate(c) for c in (candidates or [])]
    if positive_only:
        scored = [c for c in scored if c.expected_value_pct > 0 and c.expected_value_r > 0]
    scored.sort(key=lambda c: (c.portfolio_ev_score, c.expected_value_r, c.expected_value_pct), reverse=True)

    out = []
    for i, row in enumerate(scored, 1):
        payload = row.as_dict()
        payload["rank"] = i
        out.append(payload)
    return out


def portfolio_ev_summary(candidates: list[dict], positive_only: bool = True) -> dict:
    ranked = rank_portfolio_ev(candidates, positive_only=positive_only)
    return {
        "status": "AVAILABLE",
        "scope": "OBSERVATIONAL_PORTFOLIO_EV",
        "simulation_only": True,
        "broker_order_api_calls": 0,
        "candidate_count": len(candidates or []),
        "positive_ev_count": len(ranked),
        "top_candidate": ranked[0] if ranked else None,
        "ranked": ranked,
    }
