# Core Parameter Rules

Change one or two parameters per experiment.

| Parameter | Suggested Range | Increase When | Decrease When |
|---|---|---|---|
| `min_ai_confidence` | 0.55-0.75 | noise is high | qualified signals are too sparse |
| `equity_fraction_per_trade` | 0.05-0.15 | account is larger and risk budget allows | drawdown or volatility rises |
| `max_trade_notional_usd` | 10-50 | verification scale increases | safety margin is too small |
| `max_daily_loss_usd` | -1 to -5 | account size grows | capital is small or variance is high |
| `cooldown_min` | 5-60 | repeated entries are correlated | valid opportunities are suppressed |
| `dsl_exit.max_loss_pct` | 0.5-2.5 | normal noise stops the trade | losses exceed the budget |
| `dsl_exit.protect_pct` | 1.0-3.0 | winners need more room | profit protection starts too late |
| `dsl_exit.retrace_threshold` | 0.10-0.40 | exits are too early | giveback is too large |
| `scan.minCompositeScore` | 35-70 | candidate quality falls | only a few candidates surface |

## Command

```bash
hermes-solo tune min_ai_confidence=0.65
hermes-solo tune dsl_exit.max_loss_pct=1.5
```

The command writes through the existing locked configuration store, then
reads back the effective value. Do not edit a second personal YAML source.