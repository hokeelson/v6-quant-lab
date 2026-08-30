from __future__ import annotations

from .portfolio_ev import score_portfolio_candidate


def optimize_batch(candidates: list[dict], pairwise_correlation: dict[tuple[str, str], float] | None = None) -> dict[str, dict]:
    """Greedy batch allocator for simultaneous Shadow candidates.

    Candidates are ordered by portfolio EV. Positive-EV candidates retain a
    learning floor, while highly correlated lower-ranked candidates are reduced.
    Negative-EV candidates remain at a small Shadow allocation instead of being
    deleted so the research ledger can continue learning.
    """
    pairwise_correlation = pairwise_correlation or {}
    scored = []
    for raw in candidates or []:
        row = score_portfolio_candidate(raw).as_dict()
        row["requested_notional"] = max(0.0, float(raw.get("requested_notional") or 0.0))
        row["candidate_key"] = str(raw.get("candidate_key") or f"{row['market']}:{row['symbol']}:{row['horizon']}")
        scored.append(row)
    scored.sort(key=lambda x: (x["portfolio_ev_score"], x["expected_value_r"], x["expected_value_pct"]), reverse=True)

    out: dict[str, dict] = {}
    selected_symbols: list[str] = []
    best_score = max([float(x["portfolio_ev_score"]) for x in scored], default=0.0)

    for rank, row in enumerate(scored, 1):
        symbol = str(row.get("symbol") or "").upper()
        candidate_key = str(row.get("candidate_key") or "")
        ev_pct = float(row.get("expected_value_pct") or 0.0)
        ev_r = float(row.get("expected_value_r") or 0.0)
        score = float(row.get("portfolio_ev_score") or 0.0)

        if ev_pct <= 0 or ev_r <= 0:
            base_mult = 0.25
            verdict = "NEGATIVE_EV_LEARNING"
        else:
            relative = score / best_score if best_score > 0 else 0.0
            base_mult = max(0.40, min(1.0, 0.40 + 0.60 * relative))
            verdict = "POSITIVE_EV"

        cohort_corr = 0.0
        for prior_symbol in selected_symbols:
            key = tuple(sorted((symbol, prior_symbol)))
            cohort_corr = max(cohort_corr, float(pairwise_correlation.get(key, 0.0) or 0.0))
        if cohort_corr >= 0.90:
            corr_mult = 0.50
            verdict += "_HIGH_CORR"
        elif cohort_corr >= 0.75:
            corr_mult = 0.75
            verdict += "_CORR"
        else:
            corr_mult = 1.0

        multiplier = max(0.20, min(1.0, base_mult * corr_mult))
        out[candidate_key] = {
            "batch_ev_rank": rank,
            "batch_ev_multiplier": multiplier,
            "batch_ev_verdict": verdict,
            "batch_ev_score": score,
            "batch_ev_expected_value_pct": ev_pct,
            "batch_ev_expected_value_r": ev_r,
            "batch_ev_cohort_max_correlation": cohort_corr,
        }
        if ev_pct > 0 and ev_r > 0:
            selected_symbols.append(symbol)
    return out
