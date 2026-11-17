# Quick Backtest

## Goal

Validate one small strategy change with the same trigger, TA, cost and DSL code
used by SHADOW.

## Command

```bash
hermes-solo backtest --days 7 --coins-count 10 --interval 1h
```

For a smaller review:

```bash
hermes-solo backtest --days 3 --coins BTC,ETH,SOL --interval 1h
```

The command reads the runtime configuration selected by
`HERMES_AGENT_CONFIG_FILE`, then `/data/.agent-config.json`, the deployment
config, or the repository-local snapshot.

## Minimum Result Set

- loaded coins and data errors;
- signal count;
- trade count;
- win rate;
- expectancy;
- net PnL;
- max drawdown;
- exit-reason distribution.

## Acceptance

- Data must load successfully.
- A result with no trades is not a pass; it is an insufficient sample.
- Treat the result as one fast screen, not proof of durable statistical edge.