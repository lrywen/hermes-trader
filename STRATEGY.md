# Hermes SHADOW Strategy Contract

## Current Scope

- Market venue: Hyperliquid perpetuals.
- Direction: LONG-only.
- Runtime mode: SHADOW.
- HIP-3: disabled unless explicitly enabled.
- No real order may be produced by a SHADOW run.

## Decision Pipeline

1. Scan the configured universe using closed candles.
2. Calculate trigger hits and the weighted composite score.
3. Run the deterministic TA gate.
4. Only accepted LONG or long-neutral candidates reach AI research.
5. Apply the minimal personal risk policy.
6. Record a virtual fill in the Taker and Maker shadow accounts.
7. Manage the virtual position through the DSL exit policy.

## Admission

A candidate must satisfy the active composite threshold, TA gate, confidence
floor, concurrency/notional/daily-loss limits and LONG-only policy.

## Exit

The DSL policy owns hard stop, breakeven, trailing floor, time exit and stale
flat exit. Exchange-side bracket reconciliation is inactive in SHADOW.

## Change Record

Every strategy change should record:

- hypothesis;
- changed parameters;
- backtest command and configuration hash;
- SHADOW result;
- decision: keep, revert or collect more observations.