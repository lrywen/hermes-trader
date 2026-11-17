# Small-LIVE Checklist

LIVE is not enabled by the current SHADOW environment. This checklist defines
the minimum envelope for a future explicit small-capital validation.

## Hard Constraints

- `mode=SHADOW` unless a deliberate LIVE switch is requested.
- LONG-only.
- one concurrent position;
- 10 to 30 USDT per trade;
- explicit daily loss floor;
- no short-shadow arm;
- exchange stop placement confirmed after fill;
- all fills reconciled before the day is closed.

## Preflight

```bash
hermes-solo preflight
```

All checks must report `true`.

## Observation

- Verify one entry and one exit end-to-end.
- Compare exchange fill, local trade row and SHADOW expectation.
- Stop new entries immediately if health, feed freshness, stop placement or
  reconciliation fails.
- Do not force a trade when no qualified signal exists.