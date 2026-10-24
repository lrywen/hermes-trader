#!/usr/bin/env python3
"""Aggregate the weight-recalibration counterfactual (ch_weight_calib output).

Reads CH_WT_OUT jsonl (rows that pass the gate under at least one arm) plus the
.meta.json counts (which include the keep_block bulk). Reports, for the OLD
(0.55) vs NEW (0.30) weight arms:
  * quadrant counts and pass-rate share
  * forward LONG ROE (4h/12h/24h net of 9bp taker at 3x) + 24h MFE per quadrant
  * the culled group (passes OLD, blocked by NEW) broken down by BTC regime and
    by fired-trigger signature — the decisive "were the culled setups good?" cut
  * how many culled rows a rescaled gate (45 -> G', printed in meta) would
    re-admit, and their forward ROE.
Offline read-only. Env: CH_WT_OUT, HERMES_HIST_CANDLE_CACHE (for BTC regime).
"""
from __future__ import annotations

import bisect
import json
import os
import statistics as st
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

OUT = os.environ.get("CH_WT_OUT", "/data/ch_weight_calib.jsonl")
CACHE = os.environ.get("HERMES_HIST_CANDLE_CACHE", "/data/ch_hist_candles.json")


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _med(xs):
    return st.median(xs) if xs else float("nan")


def _btc_regime_stops():
    """[(1h bar t, regime)] from cached BTC 1h, trailing-100 classify (prod)."""
    from types import SimpleNamespace
    from hermes_trader.data import historical_candles as hc
    from hermes_trader.agents.market_regime import classify_candles
    hc.set_cache_file(CACHE)
    hc._load_disk_cache()
    m = hc._BAR_CACHE.get(("BTC", "1h"), {})
    bars = [m[t] for t in sorted(m)]
    stops = []
    W = 100
    for n in range(W, len(bars) + 1):
        win = bars[n - W:n]
        cds = [SimpleNamespace(t=b.t, o=b.o, h=b.h, l=b.l, c=b.c, v=b.v)
               for b in win]
        reg = classify_candles(cds)
        # regime is knowable at the close of the last bar in the window
        stops.append((win[-1].t + 3_600_000, reg))
    return stops


def _reg_at(stops, ts):
    i = bisect.bisect_right([s[0] for s in stops], ts) - 1
    return stops[i][1] if i >= 0 else "none"


def main() -> int:
    rows = [json.loads(l) for l in open(OUT) if l.strip()]
    meta = json.load(open(OUT + ".meta.json"))
    counts = meta["counts"]
    fired = counts["fired"]
    print("=" * 78)
    print("WEIGHT x min_composite CALIBRATION COUNTERFACTUAL  "
          "(mechanical TA, long, 3x, net 9bp)")
    print("=" * 78)
    print(f"sumW old={meta['sum_old']:.3f} new={meta['sum_new']:.3f}  "
          f"gate={meta['gate']}  rescaled gate G'={meta['gate_rescaled']}")
    print(f"fired 5m bars evaluated: {fired}")
    for q in ("keep_pass", "culled", "new_pass", "keep_block"):
        n = counts[q]
        print(f"  {q:11} {n:5}  {n/fired*100:5.1f}% of fired")
    old_pass = counts["keep_pass"] + counts["culled"]
    new_pass = counts["keep_pass"] + counts["new_pass"]
    print(f"OLD pass(45)={old_pass}  NEW pass(45)={new_pass}  "
          f"net admissions change={new_pass-old_pass:+d} "
          f"({(new_pass-old_pass)/max(old_pass,1)*100:+.1f}%)")

    stops = _btc_regime_stops()
    for r in rows:
        r["regime"] = _reg_at(stops, r["ts"])

    def block(sub, key):
        vals = [x[key] for x in sub if x.get(key) is not None]
        if not vals:
            return f"n=0"
        wins = sum(1 for v in vals if v > 0)
        return (f"n={len(vals):4} mean={_mean(vals):+7.2f}% "
                f"med={_med(vals):+7.2f}% win={wins/len(vals)*100:5.1f}%")

    print("\n--- forward LONG ROE by quadrant ---")
    for q in ("keep_pass", "culled", "new_pass"):
        sub = [r for r in rows if r["quad"] == q]
        print(f"\n[{q}]  ({len(sub)} rows)")
        for h in ("fwd_4h", "fwd_12h", "fwd_24h"):
            print(f"   {h:8} {block(sub, h)}")
        print(f"   {'mfe24':8} {block(sub, 'mfe24')}")
        print(f"   {'mae24':8} {block(sub, 'mae24')}")

    # culled by regime
    culled = [r for r in rows if r["quad"] == "culled"]
    print("\n--- CULLED (OLD pass, NEW block) by BTC regime ---")
    regs = {}
    for r in culled:
        regs.setdefault(r["regime"], []).append(r)
    for reg in sorted(regs, key=lambda k: -len(regs[k])):
        sub = regs[reg]
        print(f"  {reg:8} n={len(sub):4}  fwd24 {block(sub,'fwd_24h')}  "
              f"fwd4 {block(sub,'fwd_4h')}")

    # rescaled gate readmit test: how many culled have s_new >= G'
    gprime = meta["gate_rescaled"]
    readmit = [r for r in culled if r["s_new"] >= gprime]
    print(f"\n--- rescaled gate {meta['gate']}->{gprime} re-admits "
          f"{len(readmit)}/{len(culled)} culled ---")
    if readmit:
        for h in ("fwd_4h", "fwd_24h"):
            print(f"   {h:8} {block(readmit, h)}")
    print("(if few readmit, the cull is driven by trend-strength weight, not by")
    print(" the Σw normalization — rescaling the gate cannot undo it.)")

    # trigger signature of culled
    print("\n--- culled trigger signatures (top) ---")
    import collections
    sig = collections.Counter()
    for r in culled:
        sig[",".join(sorted(set(r["fired"]) &
                    {"trendStrength", "breakout", "pctMoveSpike", "volumeSpike",
                     "momentumBurst", "uptrendMomentum", "downtrendMomentum",
                     "volumeBuildup1h"}))] += 1
    for k, n in sig.most_common(8):
        print(f"  {n:4}  {k}")

    # per-coin cull count (is it one noisy coin?)
    cc = collections.Counter(r["coin"] for r in culled)
    print("\n--- culled by coin (top 10) ---")
    for k, n in cc.most_common(10):
        print(f"  {k:10} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
