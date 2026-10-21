# Event-Time Replay — Synthetic Fill Buckets by Surface Channel

**Date:** 2026-10-20 (UTC)
**Purpose:** price the forward-return findings (`ch_channel_forward_returns_2026-10-20.md`) through the **production exit state machine and taker costs**, turning historical point-in-time signals into synthetic closed trades — the "large sample" step that avoids waiting months for real tagged fills.
**Artifacts:** `scripts/ch_event_replay.py`, `tests/test_ch_event_replay.py`, trades `/tmp/ch_trades.jsonl` (container).

## Method (and why it is credible)

- **Signals:** same 33,849 dedup `(coin,15m)` surfaced set; trade only AI-verdict LONG/SHORT → **11,993 synthetic closed trades / 174 coins** (9 right-censored, excluded). 8,544 long / 3,449 short.
- **Entry:** open of the first 15m bar at/after the signal (one-bar conservative lag), taker fill.
- **Exit:** the **real `dsl_exit.DSLTracker`** driven bar-by-bar with the production config-derived `ExitPolicy` (`protect 1.5%`, hard timeout 240m, stale/ignite grace 90m, `atr_stop_enabled=false`), wall-clock pinned to bar time, persistence disabled. Bars are fed **adverse-tick-first** (long: low then high) so stop fills are conservative; floor exits fill at the tracked floor (gap bounded to that bar's range).
- **Launch gate replicated:** before each stop check, a post-entry 15m bar meeting the production ignition rule (volume ≥2× prior-20 mean **and** close beyond their range) calls `tracker.mark_ignited`, so positions that launch survive the 90-min grace — matching `dsl_exit.detect_ignition`.
- **Costs:** taker 0.045% × 2; ROE at 3× leverage. Funding omitted (short holds); maker fills omitted (taker-only = conservative).

**Fidelity check against real money:** the replay reproduces the bimodal exit signature seen in the 218 real SHADOW closes (P0-A):

| exit | synthetic median ROE (win) | real 218-fill signature |
|---|---|---|
| max_loss | **−7.33% (0%)** | hard-loss cluster |
| floor_breach | **+3.37% (81.5%)** | trailing-stop winner cluster |
| stale_flat_timeout (68%) | −3.26% (14%) | un-ignited drifters pruned at ~45–90m |

Same shape, same driver (which exit arm fires), so the simulator is exercising the real payoff mechanics rather than a stylized model.

## Result — per channel (3× ROE, net of taker round-trip)

| channel | n | median ROE (95% CI) | win | coin-clustered mean ROE / % coins positive (LONG) |
|---|--:|---|--:|---|
| **gate (≥54)** | 32 | **−0.26%** [−1.82,+1.53] | 50.0% | **+0.70% / 58.8%** (17 coins) |
| trend | 9,230 | −2.26% [−2.36,−2.16] | 31.1% | −2.17% / 9.3% (150) |
| mover | 1,729 | −2.08% [−2.27,−1.88] | 28.5% | **−2.53% / 9.1%** (55) |
| pattern | 819 | −1.90% [−2.29,−1.47] | 32.6% | — |
| burst | 183 | −3.98% [−5.16,−2.32] | 36.6% | — |

Channel × side mean ROE: gate/LONG **+0.75%** is the only positive cell; trend/LONG −1.94%, trend/SHORT −1.88%, mover/LONG −1.96%, mover/SHORT −1.37%. Only **9–12%** of trend/mover setups ever reach the trailing/held path (vs 22% gate, 25% burst) — the bypasses overwhelmingly surface setups that never launch and get pruned at a small, fee-and-drift-negative timeout.

## Portfolio implication (the important caveat)

The bypass channels are individually bad, but the whole book is negative, so fixing one channel is **not** a path to profitability:

| portfolio | mean ROE/trade |
|---|--:|
| all 11,993 | −1.874% |
| drop mover-LONG | −1.862% |
| drop mover + burst | −1.892% |
| LONG only (the production-allowed side) | −1.887% |
| LONG ex-mover | −1.872% |

Removing mover-LONG barely moves the mean (it is 13% of trades). The loss is **broad-based in the bypass-admitted long flow** (trend is the majority and also ≈−2%), not concentrated in dailyMover. dailyMover is the *cleanest* target statistically (forward-return analysis: survives de-meaning, every horizon, coin-clustered), but tightening it alone trims a little cost — it does not create edge.

## Decision

1. **NO-GO for LIVE is reinforced:** after the real DSL exits and taker costs, every dominant bypass channel is negative on independent coin clusters; the ≥54 gate is flat-to-slightly-positive but tiny (n=32) and CI crosses zero. Nothing here clears a go-live bar.
2. **Tightening dailyMover is supported but only as cost control** (e.g. require a sub-floor composite or confirmation for dailyMover-only surfacing). Expect a small drag reduction, not a flip.
3. **The larger, evidence-backed move is to make surfacing more selective overall** — raise the quality bar on trend-only / low-composite admissions (the 74.7% majority that is −2% and 92% un-ignited), not just mover. This should be done as a config/experiment, then **validated on real tagged fills** (the fired_names instrumentation now live), because:
4. **Do not trade off the replay alone.** Approximations: 15m intrabar path (stop fills bracketed, not tick-exact), funding ignored, AI verdict coverage ≈61% (async join), taker-only, and the replay assumes every LONG/SHORT verdict becomes a fill (no runner/risk-gate filtering, so it is an upper bound on how much of this flow would ever be traded). It is a ranking/go-no-go instrument; real tagged SHADOW fills remain the deployment gate.

## Reproduce

```
# inside container, offline against the 15m candle cache
CH_SIGNALS=/tmp/ch_signals.json CH_TRADES=/tmp/ch_trades.jsonl \
HERMES_HIST_CANDLE_CACHE=/tmp/ch_candles.json REPLAY_LEV=3 \
  python3 scripts/ch_event_replay.py
```
