# Historical replay of the redundancy-plan B-class questions

Date: 2026-10-21
Mode: offline, network-free analysis (no production trade path touched).
Question: the 2026-10-21 redundancy/L3-L5 plan defers B-class knob changes until
~7 days of structured shadow fills accumulate. Can history answer them now?
Answer: **partially — yes for the exit knobs and entry-path ranking; directional
only for the cooldown arms. No variant turns the long-only bypass flow net
positive, so the LIVE NO-GO is unchanged. One exit knob (protect_pct) shows a
consistent, mechanistically-explained improvement and is the leading candidate
for the first gated live test.**

## Data

* Signals: 8,264 LONG PIT signals from the surviving `ch_event_replay` trades
  (2026-09-19 → 2026-10-20, 31 days; SHORTs excluded — the SHADOW book is long-
  only and prior work found no short edge).
* Bars: freshly rebuilt deep cache, **153 non-HIP-3 coins × ~3,841 15m bars**
  (2026-09-11 → 10-21, 572,328 bars), covering 97% of replay trades. HIP-3
  `xyz:` coins excluded (340 trades / 2.8%; dead-socket fetches, illiquid).
* Engine: production `dsl_exit.DSLTracker` driven bar-by-bar adverse-tick-first
  (same as `ch_event_replay`), taker 0.045%/side, 3x. **Same entries are reused
  across every exit variant → a paired causal comparison** (only the policy
  differs; identical intrabar path and fills).
* Regime: reproduced the **production cross-sectional semantics** — for crypto
  the live system classifies the **BTC proxy** on the trailing 100 CLOSED 1h
  candles, then applies that one regime to every coin. We aggregate BTC 15m→1h
  and run the same pure `classify_candles` at every signal time (full coverage:
  up 36%, chop 34%, down 16%, neutral 14%).

Artifacts: `scripts/ch_exit_param_grid.py` (grid json `/tmp/v2/ch_grid_result.json`),
`scripts/ch_feature_table.py` (`/tmp/v2/ch_features.jsonl`).

## 1. Exit-parameter grid (B1 protect_pct, B4 breakeven) — the highest-value result

8,264 LONG entries × 5 policy variants, median/mean taker ROE net of fees:

| variant | med ROE | mean ROE | win | phase2 armed | exits stale→floor |
|---|---:|---:|---:|---:|---|
| baseline (protect 1.5) | −2.13% | −1.486% | 34.4% | 42.3% | 60.7% / 19.4% |
| protect 1.0 | −1.96% | −1.349% | 36.0% | 48.8% | 54.1% / 23.7% |
| **protect 0.75** | **−1.72%** | **−1.192%** | **37.8%** | 56.2% | 46.6% / 28.7% |
| breakeven 1.0 only | −2.13% | −1.486% | 34.4% | 42.3% | identical to baseline |
| protect1.0 + BE1.0 | −1.96% | −1.349% | 36.0% | 48.8% | identical to protect1.0 |

**Finding 1a — lowering protect_pct is monotone-beneficial but not sufficient.**
1.5→0.75 lifts median ROE +0.41pp, win +3.4pp, and converts **stale-flat timeouts
into floor_breach exits** (stale 60.7%→46.6%; floor 19.4%→28.7%). Mechanism:
trades that peak 0.75–1.5% then drift flat are currently stale-cut with no
profit lock at ~flat/small loss; arming phase-2 earlier trails the peak and
recovers a small win. **But every variant is still net negative** — the entry
edge is missing, so a better exit only narrows the bleed; it cannot manufacture
profit.

**Finding 1b — the B4 breakeven ratchet (2.5→1.0 candidate) is a NO-OP under the
current 90-min stale clock.** `breakeven_1.0` is byte-identical to baseline and
the combined variant identical to protect1.0 — the BE floor never changes a
single exit across 8,264 sims. Cause is exact, not speculative: of signals whose
24h MFE enters the BE arming band [1.0%, 1.5%), **98% (392/400) exit via
stale_flat_timeout / max_loss before any round-trip through the +0.2% floor**;
6,721 signals clear protect 1.5% anyway (BE is subsumed by phase-2 there) and
1,143 never reach +1.0% (BE never arms). The 90-min stale clock exits the
in-between trades on **time**, so a tighter price floor cannot bind. The lever
that matters is `protect_pct` (which also grants the stale-timeout exemption and
arms phase-2), not an independent breakeven lock at the current stale timeout.

## 2. MFE by regime (V-5: "wrong entry" vs "stop too tight")

Bar-based favorable/adverse excursion, first 24h, by BTC regime:

| regime | n | MFE med | MAE med | fwd24 med | win24 |
|---|---:|---:|---:|---:|---:|
| up | 3,013 | +4.48% | −6.46% | **−3.16%** | 33.4% |
| chop | 2,784 | +4.65% | −4.75% | −1.83% | 37.9% |
| down | 1,285 | +5.36% | −5.25% | −0.67% | 46.5% |
| neutral | 1,182 | +6.25% | −4.66% | **+0.57%** | 52.6% |

Realized replay exits reproduce the double peak: max_loss ROE −7.35%/win 0
(n=232) vs floor_breach +3.30%/win 81 (n=729) vs hard_timeout +10.26%/win 91
(n=408) vs ignite_timeout +1.82%/win 63; stale_flat −3.68%/win 12.

**Finding 2a — max_loss is NOT uniformly "wrong at entry".** max_loss-exit
trades show median 24h MFE +2.46%, and **51–74% of them at some point print a
favorable move ≥ +1.5%** (up 51%, chop 66%, down 51%, neutral 74%). A large
fraction first moved the right way, then reversed through the ~1.5% neutral
spot stop. This is **stop-tightness / round-trip**, not pure bad-pick, and is
exactly the population a slightly earlier arm (finding 1a) addresses. It
contradicts the small real-ledger note "max_loss MFE max = 1.96%": that figure
was peak-at-exit on 59 trades; the bar-based *intra-hold* MFE is materially
higher (real wick/floor timing is stricter than bar extremes, so treat the bar
MFE as an upper bound).

**Finding 2b — entries in a BTC UP regime are the WORST (median fwd24 −3.16%);
NEUTRAL is the only positive (+0.57%, win 53%).** Trend-channel breakouts bought
*after* an uptrend is established chase extended moves (the "追在行情末端"
thesis). The same pattern holds inside trend (up −3.12 vs neutral +1.48) and is
extreme for burst (up −6.95 vs neutral +5.20, small n=18). A regime-gated entry
tighter in `up` (or requiring fresh impetus there) is more promising than exit
tuning — but it is an entry gate change and stays gated behind real fills.

## 3. Entry-path historical buckets (V-2 / B1 score leg / mover)

24h forward close, by mutually-exclusive surface channel:

| channel | n | fwd24 med | mean | win |
|---|---:|---:|---:|---:|
| gate (≥54) | 17 | −0.34% | +1.03% | 41% |
| pattern | 616 | −0.91% | −0.11% | 43.3% |
| trend | 5,953 | −1.47% | −0.52% | 42.0% |
| burst | 134 | −3.12% | −2.20% | 38.1% |
| mover | 1,486 | **−3.46%** | −2.62% | 29.1% |

Consistent with the earlier `ch_bucket_forward_returns` result: mover is the
cleanest leak, gate near-flat (but n=17 historically), trend (the volume driver)
negative. Composite-band stratification is non-monotone over the resolved-trade
subset (`<25` −1.16, `25-35` −2.49, `35-45` −1.73, `45-54` −2.63, `>=54`
−2.15) — i.e. among signals strong enough to reach a synthetic fill, the score
alone does not rank quality; regime/channel dominate.

**Limitation (V-2 not fully answerable from history):** the `fresh_impulse`
sub-legs — breakout-this-bar vs the 30-min `recent_breakout` implicit leg vs
vol-burst vs score-only — require the L1 `fired_names` vector, which is not
present in pre-2026-10-20 logs. Only the coarser `channel` label survives. The
new `runner_gate_shadow.jsonl` (`fresh_src`, `runner_carrier`) is the first
source that records the sub-legs; that specific contrast genuinely needs the new
instrumentation, not a deeper cache.

## 4. Cooldown-arm counterfactual (R3 / V-1) — directional only

At signal granularity, same-coin LONG re-surfaces are dense (median gap ~17 min;
95% of follow-ups within 24h) — but scans re-emit the same move, so these are
not distinct attempts. Restricting to the economically meaningful comparison, a
repeat same-coin LONG within 24h vs the first-in-window:

* signal fwd24: repeat −1.90% (win 39.4%) vs first −1.12% (win 42.8%) → **−0.89pp**
* replay ROE: repeat −2.44% (win 30.2%) vs first −2.06% (win 29.9%) → −0.36pp
* blocked-repeat mix: trend 5,599 / mover 1,476 / pattern 536 / burst 120 / gate 5.

Direction: **the cooldown blocks worse re-entries, not winners** (the repeat flow
is dominated by trend/mover, the two negative channels). Nothing here supports
*shortening or deleting* a cooldown; if anything the repeat entries are lower
quality. **Quantitative limit:** `ch_trades` replays each signal independently
without enforcing live cooldown/slot exclusivity, so it is not a realistic
sequential position stream. A definitive count ("arm X would block N distinct
fills, M of which would have won") needs a **gated sequential replay** (stateful
cooldown + max-concurrent), which the new front-loaded cooldown shadow record
(`per_coin_cooldown_shadow.jsonl`) is already designed to inform from live data.

## Decision

1. **LIVE NO-GO maintained.** No exit variant flips the long-only bypass flow
   net positive (best variant median ROE −1.72% / mean −1.19% net of fees). The
   leak is entry quality (esp. chasing in BTC-up regimes and the mover channel),
   not the exit.
2. **`protect_pct` 1.5→~1.0 is the best-supported first B-class live test**:
   monotone across median/mean/win, mechanistically explained (stale→floor
   conversion), risk-reducing in spirit (earlier profit protection). It is a
   *trade-behaviour* change, so do NOT flip it on this simulation alone — run it
   as a config A/B behind the now-landed per-exit shadow writer and confirm on
   real tagged fills. 0.75 is more aggressive; 1.0 is the prudent first step.
3. **Do not prioritise the independent breakeven knob** at the current 90-min
   stale timeout — it is mechanically a no-op. If stale timeout is later
   lengthened, BE can bind and should then be re-tested.
4. **Highest-upside future evidence is entry-side regime gating** (tighten in
   BTC-up; neutral is the only positive regime), plus the known mover suppression
   — both still gated behind real `fired_names` fills.
5. Cooldown arms: history gives no reason to loosen; defer the exact arm
   redundancy verdict to the new structured shadow stream / a gated sequential
   replay.

## Addendum — regime-FAITHFUL re-test against the actual production policy

The first grid varied protect against one neutral baseline (protect 1.5, stale
90). Checking the live `/data/.agent-config.json` showed production actually
**overlays per regime** via `overlay_regime_exit`, so that baseline was wrong:

* `up`/`down` → trend_ride: **protect 2.5**, retrace 0.4, stops 4.0%/20 ROE,
  stale **120**, hard 240
* `chop`/`neutral` → non_trend: **protect 1.5**, stops 1.5%/15 ROE,
  stale **60**, hard 120

`ch_exit_param_grid_regime.py` rebuilds each signal's REAL policy from the live
dsl block + its BTC-proxy regime and re-runs the paired grid (`ch_grid_regime_result.json`,
8,264 entries; trend bucket n=4,298, non_trend n=3,966):

| variant | overall med ROE | mean | win | arm | non_trend med / win | trend med / win |
|---|---:|---:|---:|---:|---|---|
| baseline (production) | −2.40 | −1.828 | 32.7% | 31.5% | −2.20 / 32.3% | −2.71 / 33.0% |
| non_trend protect→1.0 | −2.28 | −1.701 | 34.2% | 37.6% | **−1.93 / 35.5%** | unchanged |
| non_trend protect→0.75 | −2.19 | −1.634 | 35.2% | 41.2% | −1.71 / 37.6% | unchanged |
| trend protect 2.5→1.5 | −2.23 | −1.671 | 34.0% | 40.3% | unchanged | **−2.27 / 35.6%** |
| **combined (trend1.5 + nontrend1.0)** | **−2.09** | **−1.544** | **35.6%** | 46.4% | −1.93 / 35.5% | −2.27 / 35.6% |

The improvement survives the corrected policy and appears in BOTH regimes:
lowering protect converts stale-timeout cuts into floor_breach exits
(overall stale 33.2%→26.6%, floor 11.0%→18.2%). Magnitude is modest —
**+0.28pp mean ROE per 3x trade (~0.095% gross), +2.9pp win rate (~241 flipped
trades / 31d)** — and **every arm is still net negative**.

### Can trading behaviour be changed directly now? — NO (but a narrow, reversible exception)

Evidence FOR a direct change: large paired sample (8,264, same entries), correct
production regime policy, monotone in both regimes, exact mechanism, and the
change is risk-REDUCING in intent (lock profit earlier), not a leverage/size
increase.

Evidence AGAINST flipping it straight into the binding LIVE exit:

1. **The system is in SHADOW and unproven end-to-end; there is no LIVE capital
   yet.** A protect_pct change cannot improve realised P&L until LIVE exists, and
   SHADOW simulation is not fills.
2. **Simulation fidelity gaps are largest exactly where this knob acts.**
   protect/arm/floor outcomes depend on sub-15m path, wick vs floor timing, and
   the oracle index confirmation; the replay feeds two ticks/bar (adverse first)
   and approximates fills. bar-MFE is an upper bound vs real floor execution, so
   the "would have armed earlier" rate can be overstated. The +0.28pp/trade edge
   is well inside plausible fill-model error.
3. **It does not fix the NO-GO cause.** Best case leaves the strategy mean
   −1.54%/trade net negative; tuning the exit while entries are negative is
   polishing the leak, not the source.
4. **Real tagged fills for the arms are still <5.** The whole P0 instrumentation
   rationale was that these decisions should be made on realised exits.

**Recommended path (reversible, no LIVE risk):**
land protect_pct as a SHADOW-ONLY config A/B using the per-exit shadow writer
already deployed — run TWO shadow policies (production vs trend1.5/nontrend1.0)
on the same live signals and compare REAL tagged exit outcomes for ~2–4 weeks
(the dual-policy evaluation is simulation-free because both arms observe actual
market prints; only fills are paper). Promote to LIVE only if the real-outcome
arm reproduces the direction. Do not edit the binding exit before that. This
keeps "evidence before behaviour change" while no longer waiting passively.


## Reproduce

```
# 1) deep cache (153 non-HIP-3 coins, 40d 15m; once)
docker cp scripts/collect_candles.py hermes-trader:/tmp/
docker exec -w /app hermes-trader python3 /tmp/collect_candles.py \
  --interval 15m --days 40 --cache-file /data/ch_deep_candles.json \
  --coin $(paste -sd' ' coins_nohip.txt) --sleep 0.1
docker cp hermes-trader:/data/ch_deep_candles.json /tmp/v2/

# 2) feature table (fwd returns, bar MFE/MAE, production BTC regime)
CH_TRADES=/tmp/v2/ch_trades.jsonl CH_DEEP=/tmp/v2/ch_deep_candles.json \
CH_FEATURES=/tmp/v2/ch_features.jsonl \
  python3 scripts/ch_feature_table.py

# 3) exit-policy grid (paired, production DSLTracker)
CH_TRADES=/tmp/v2/ch_trades.jsonl CH_DEEP=/tmp/v2/ch_deep_candles.json \
CH_GRID_OUT=/tmp/v2/ch_grid_result.json \
  python3 scripts/ch_exit_param_grid.py
```

## Caveats

Simulation, not fills: 15m intrabar path approximated (adverse-tick-first is
conservative for stops but cannot model sub-bar MFE timing), taker-only (maker
ignored), funding omitted, 3x fixed, signal-time policy neutral baseline; bar
MFE is an upper bound vs real floor/wick execution; regime is the BTC proxy
(production semantics), not per-coin; cooldown results are signal-level, not an
enforced sequential order book.
