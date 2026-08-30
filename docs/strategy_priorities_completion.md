# V6 strategy-priority completion

The five strategy-quality priorities are now represented in the Paper/Research sizing stack:

1. Trade EV is active in virtual sizing.
2. Dynamic regime allocation applies a conservative 0.70–1.00 strategy prior.
3. Forward/Shadow evidence progressively dominates historical OOS, reaching 90% Shadow / 10% OOS at 30+ closed trades.
4. Insufficient-history conditions are WAITING_DATA rather than TRUE_ERROR.
5. Batch Portfolio EV ranks simultaneous candidates and reduces lower-ranked highly correlated candidates.

All new allocation logic is Shadow/Paper only. Broker order API calls remain 0 and no live broker authorization is introduced.
