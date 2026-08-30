# Active EV sizing

V6 now uses Trade EV and Portfolio EV in virtual position sizing.

- Negative EV is reduced rather than fully blocked so Shadow learning can continue.
- Strong positive EV can keep full model size.
- Portfolio EV discounts candidates with high correlation or concentrated projected exposure.
- Overlapping evidence is de-duplicated with conservative `min()` combinations instead of multiplying the same evidence repeatedly.
- Existing Forward/Shadow-vs-OOS weighting remains active and can dominate mature strategy sizing.
- This remains simulation-only. Broker order API calls remain 0.
