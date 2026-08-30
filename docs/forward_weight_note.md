# Forward / Shadow evidence weighting

V6 keeps historical OOS as the anchor while Shadow evidence is immature, then progressively shifts confidence toward realized Shadow performance.

- <5 closed Shadow trades: 0% Shadow / 100% OOS
- 5 closed trades: 25% Shadow / 75% OOS
- 30+ closed trades: 90% Shadow / 10% OOS

The weighting only changes virtual position sizing through the existing expected-live multiplier. It does not enable broker orders or alter the broker-order safety invariant.
