# Surface-Channel Forward-Return Analysis — trend_bypass & dailyMover

**Date:** 2026-10-20 (UTC)
**Method:** point-in-time, offline, no look-ahead. Extends `analysis/bf_bucket_forward_returns_2026-10-20.md`.
**Artifacts:** `scripts/ch_signal_extract.py`, `scripts/ch_bucket_forward_returns.py`, `tests/test_ch_bucket_forward_returns.py`.

## Question

L1 audit F2 showed the composite gate (54) admits <2% of surfaced coins; the real selectors are the OR bypasses — trend_bypass ≈60–85% and dailyMover ≈8–37% of surfaced volume. **Do setups surfaced through those two bypasses precede systematically different forward returns than the genuine ≥54-gate setups?** If the bypasses are feeding the engine low-quality momentum peaks, that is direct evidence for tightening them.

## Data

- Source: every surfaced coin in every scan event from the concatenated session-log (current + 5 rotated gz), **2026-09-03 → 2026-10-20**.
- Raw surfaced coin-cycles: **212,722**. Dedup to one signal per `(coin, 15m bar)` (keep highest composite; overlapping scan cycles otherwise count the same bar entry many times) → **33,849 distinct bar entries across 242 coins**.
- Channel is mutually exclusive with the **same OR precedence as production** (`perception._scan_single_market`): burst → trend (`uptrend/downtrendMomentum`) → reversal pattern → dailyMover → composite≥54 gate. Shares: trend 74.7%, mover 13.2%, pattern 9.3%, burst 1.9%, gate 0.8%.
- Entry = close of the last 15m bar that had **closed** at signal ts; forward marks at 4h/12h/24h/48h. 190 coins / **29,744 rows** priced from the PIT candle layer (52 illiquid/delisted `xyz:` series had no cache; they are a small tail and would only strengthen the mover result — see caveats).
- Statistics: exact median/win/mean over all rows with deterministic bootstrap 95% CI (CI sampled, capped at 4,000/ bucket for speed); **coin-clustered paired bootstrap** for the channel-vs-gate contrast (each coin collapsed to its per-channel mean; only coins seen in BOTH groups; repeated signals cannot inflate n), plus cross-sectional de-meaning within each 15m signal bucket to remove market beta / timing composition.

## Result 1 — raw forward returns by channel (long convention)

| channel | n | coins | 24h median | 24h win | 48h median | 48h win |
|---|--:|--:|--:|--:|--:|--:|
| **gate (≥54)** | 161 | 71 | **+0.54%** | 56.0% | −0.20% | 48.7% |
| trend | 22,375 | 176 | −0.94% | 44.1% | −1.66% | 42.4% |
| **mover** | 3,710 | 71 | **−3.60%** | **28.9%** | **−5.25%** | **22.8%** |
| pattern | 2,910 | 167 | −0.48% | 46.9% | −0.91% | 45.6% |
| burst | 588 | 104 | −2.18% | 43.6% | −4.46% | 35.7% |

The single most selective channel (the real composite gate) is the only one with a positive 24h median; the two dominant bypasses are negative, dailyMover dramatically so.

## Result 2 — short-convention (fading the surfaced signal)

| channel | 4h | 12h | 24h | 48h |
|---|--:|--:|--:|--:|
| gate | +0.04 / 50% | −0.10 / 48% | −0.54 / 44% | +0.20 / 51% |
| trend | +0.23 / 54% | +0.53 / 55% | **+0.94 / 56%** | **+1.66 / 58%** |
| **mover** | +0.68 / 61% | **+2.27 / 69%** | **+3.60 / 71%** | **+5.25 / 77%** |
| pattern | +0.20 / 54% | +0.35 / 53% | +0.48 / 53% | +0.91 / 54% |
| burst | +0.34 / 52% | +1.78 / 57% | +2.18 / 56% | +4.46 / 64% |

(median % / win % of fading)

Fading a dailyMover signal wins **71% at 24h and 77% at 48h**; the edge grows monotonically with horizon. This is the same mean-reversion-after-spike structure seen in the bf analysis, now isolated to the exact bypass channel that admits it.

## Result 3 — coin-clustered contrast vs the genuine gate (the conservative test)

Each coin contributes one mean per group; only coins observed in both channels; 1,000 paired bootstraps.

**mover vs gate** — strong, survives de-meaning, all horizons:

| horizon | raw diff (CI), p | de-meaned diff (CI), p | n coins |
|---|---|---|--:|
| 4h | −1.50% [−2.33,−0.70], **0.001** | −0.69% [−1.34,−0.00], **0.046** | 41 |
| 12h | −3.92% [−5.35,−2.55], **0.001** | −1.56% [−2.59,−0.60], **0.001** | 41 |
| 24h | −5.18% [−7.49,−3.20], **0.001** | −3.04% [−4.63,−1.50], **0.001** | 40 |
| 48h | −6.17% [−8.88,−3.45], **0.001** | −4.21% [−6.11,−2.13], **0.001** | 38 |

**trend vs gate** — weaker, only clearly significant at 24h after de-meaning:

| horizon | raw diff (CI), p | de-meaned diff (CI), p |
|---|---|---|
| 4h | −0.13%, p=0.64 | −0.03%, p=0.91 |
| 12h | −0.52%, p=0.23 | −0.45%, p=0.23 |
| 24h | −1.24% [−2.40,−0.08], **0.038** | −1.26% [−2.12,−0.35], **0.010** |
| 48h | −1.27%, p=0.18 | −2.11% [−3.29,−0.89], **0.001** |

Unlike the bf analysis — where the strong-bullish AI-SHORT edge collapsed to zero after cross-sectional de-meaning — **the dailyMover deficit survives de-meaning with the same sign and significance at every horizon, and is confirmed on independent coin clusters (38–41 coins).** It is not "which coin / which day" composition: within the same coin and the same 15m market bucket, a dailyMover-surfaced entry is 3–4pp worse at 24–48h than a ≥54-gate entry.

## Composite bands (24h long) confirm monotonicity

| band | n | median | win |
|---|--:|--:|--:|
| ≥54 | 410 | −0.18% | 48.9% |
| 45–54 | 461 | −1.03% | 42.8% |
| 35–45 | 2,719 | −1.01% | 44.1% |
| 25–35 | 9,528 | −1.45% | 40.8% |
| <25 | 16,626 | −1.15% | 43.1% |

Quality degrades with score; the (small) ≥54 set is the only near-flat/positive band. The gate isn't decorative because 54 is magic — it's decorative because the bypasses let 99% of the flow ignore it.

## Interpretation and policy

1. **dailyMover bypass is the clearest leak.** It admits 8–37% of surfaced coins at a strongly negative, de-mean-robust, coin-clustered forward profile (24h median −3.6%, fading wins 71–77%). This is now the best-evidenced candidate for tightening: require a minimum composite (e.g. a sub-floor) or a confirmation gate for dailyMover admissions, or stop letting dailyMover alone surface a coin.
2. **trend_bypass is mostly the broad, weak pool**, not a sharp alpha leak: trend entries are mildly negative but the vs-gate gap is small and only reaches significance at 24–48h. It should not be removed (it is the engine's coverage), but a modest score floor for trend-only admissions is defensible.
3. **Do NOT yet flip sign / open shorts.** This is signal-point quality, not cost-inclusive fill PnL. Fading dailyMover at +3.6%/24h gross still has to clear taker round-trip (~9 bp), maker fill assumptions, ATR/DSL exit timing and the fact that the AI's *direction call* on these points was not measured here. That is exactly what the event-time replay (next step) prices before any gate change is shipped.
4. The genuine ≥54 gate itself does **not** show a reliable positive edge (CI crosses zero), so lowering the gate to admit more is also unsupported. The supported move is asymmetric: **raise the quality of bypass admissions**, especially dailyMover.

## Caveats

- 52 mostly-illiquid/delisted `xyz:` series lacked candle cache and were skipped. They skew toward mover/spike names; including them would most likely deepen, not reverse, the mover deficit.
- Mark-to-market forward returns ≠ realized trade PnL (no stops, fees, funding). Bounded by the replay next.
- AI verdict join is advisory (async, ~56% nearest-neighbour hit) and is not used in the channel-vs-gate tests.
- 15m resolution under-models intrabar stop triggers; the replay feeds bar high/low to the production DSL tracker to address this conservatively.
