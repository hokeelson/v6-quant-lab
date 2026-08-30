# Portfolio EV ranking

V6 now has a portfolio-level EV ranking primitive for simultaneous candidate trades.

The ranking uses risk-normalized trade EV, evidence quality, current confidence, correlation penalty, and existing exposure penalty. Positive-EV candidates are ranked from strongest to weakest.

This layer is observational only in this phase. It does not authorize entries, place broker orders, or change the broker-order safety invariant (`broker_order_api_calls = 0`).
