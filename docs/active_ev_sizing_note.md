# Active EV sizing

V6 now uses Trade EV and Portfolio EV in virtual position sizing.

- ENTRY_GATE_V1 separates BUY admission from sizing. Mature negative EV (existing
  evidence weight >= 0.25) vetoes new entries; immature EV remains a sizing warning.
- Strong positive EV can keep full model size.
- Portfolio EV discounts candidates with high correlation or concentrated projected exposure.
- Overlapping evidence is de-duplicated with conservative `min()` combinations instead of multiplying the same evidence repeatedly.
- Existing Forward/Shadow-vs-OOS weighting remains active and can dominate mature strategy sizing.
- This remains simulation-only. Broker order API calls remain 0.

## ENTRY_GATE_V1

- BLOCK_CANDIDATE, SHADOW_ONLY / SHADOW_ONLY_CANDIDATE, PAUSE_CANDIDATE
  and QUARANTINED now veto new paper positions, rather than merely reducing size.
- Zero multipliers remain zero; nonfinite/out-of-range multipliers and assessment
  errors fail closed. The old V6_MIN_ACTIVE_SIZE_MULTIPLIER floor is no longer used.
- Existing pending BUYs are assessed at fill. Cancelled entries retain their original
  decision and a version-tagged ORDER_CANCELLED diagnostic with explicit reasons.
- SELL, protective/time exits, cash, existing positions and historical records are
  unchanged. No automatic liquidation, short execution, account reset or V10 takeover.
- V10 continues independent observations. A blocked legacy strategy does not receive
  a funded learning allocation; recovery/promotion needs separately reviewed evidence.
- research_snapshot.json exposes entry_gate with sampled blocked/filled counts and
  blocked_but_filled violations. WAITING_FOR_EVENTS is not proof of live admission.
- Policy version is attached to admission diagnostics, not retroactively to old PnL.
  Tests validate behavior, not profitability.
