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
- Treat the result as one fast screen, not proof of durable statistical edge.- A background job must end as `completed` with `exit_code=0`; `failed`, `lost`, or a live PID after an unreasonable runtime requires log inspection.

## Background Jobs

For a non-blocking run:

```bash
hermes-solo backtest --days 7 --coins-count 10 --interval 1h --background
# job=bt-...
hermes-solo backtest --result bt-...
hermes-solo backtest --result bt-... --json
```

The background command returns immediately. Status, worker PID, exit code, log
path and result are stored under the system temporary directory by default
(`/tmp/hermes-solo-jobs` on the server). Set `HERMES_SOLO_JOB_DIR` to use a
separate scratch directory. Quick backtests never write to `/data` runtime state.
