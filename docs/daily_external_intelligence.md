# Daily External Intelligence Layer

V6 now maintains a public, read-only external intelligence snapshot for stock, Taiwan stock, and crypto research.

## Purpose

This layer adds daily macro/news/event context without allowing an LLM or headline to directly authorize a trade. The existing strategy, EV, Forward/Shadow evidence, portfolio risk, and leverage guards remain authoritative.

## Inputs

The first version uses public, no-key sources:

- Google News RSS queries for macro / Federal Reserve / inflation / jobs / Treasury / stock-market headlines.
- Google News RSS queries for Bitcoin / Ethereum / crypto ETF / SEC / crypto security headlines.
- Yahoo Finance chart endpoints for VIX, US 10Y yield, DXY, S&P 500, and BTC-USD context.

The worker refreshes every six hours. The public snapshot is `static/daily_external_intelligence.json`. A JSONL history is also appended under the persistent data directory when available.

## Safety policy

External intelligence is reduction-only in the first version:

- It can reduce a virtual position or reduce a strategy's daily weight.
- It cannot increase exposure above the original 1.00x model size.
- It cannot create an ENTER signal.
- If sources fail, coverage is poor, or the snapshot is older than 12 hours, the layer fails open to 1.00x instead of inventing a risk signal.
- Broker order API calls remain 0.

## Scoring

Each market context includes:

- `risk_regime`
- `risk_score`
- `sentiment_score`
- `event_risk`
- `market_stress`
- `confidence`
- `market_multiplier`
- per-strategy multipliers for Trend MA, Momentum, Mean Reversion RSI, and Breakout.

The pre-trade layer uses the strictest of the ordinary portfolio-risk multiplier and the external-intelligence reduction. Dynamic regime and Batch Portfolio EV then remain de-duplicated through the existing `min()` overlay.

## Evaluation

This is intentionally a forward research feature. Daily snapshots are retained so later analysis can test whether the external layer improved:

- average trade return
- profit factor
- win rate
- drawdown
- EV_R threshold cohorts
- Policy Epoch performance

If the layer has no measurable forward value, its weight should be reduced or disabled rather than justified from narrative examples.
