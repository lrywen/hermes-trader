#!/usr/bin/env python3
"""Stage-2 (2026-10-30): path A (fresh & slow>=1) forward evaluation.

Answers ONE pre-registered question: should the path-A bypass
(``fresh_impulse AND slow_burn_count>=1``) be enforced-blocked? Path A is the
dominant entry carrier (~69% of admitted longs) yet its composite score barely
participates. Path C (daily-mover-only) was already enforced-blocked on a
negative-edge verdict; this script applies the SAME forward methodology to
path A without presuming the same conclusion.

Data (already produced offline, NOT re-fetched):
  * decision_shadow_replay_bn90d.jsonl  — 100k long decision rows replayed
    through the REAL executor._runner_entry_block_reason_impl over 88 days of
    Binance 5m candles (deterministic structural inputs; LLM conf neutralized).
  * bn90_outcomes_<h>.jsonl            — forward net19/excess19 returns joined
    on (coin, ts), already path-bucketed.

Pre-registered readout (no post-hoc tuning):
  * primary metric: excess19 (carrier edge net of equal-weight market beta);
    gross/net19 reported as reference.
  * primary horizon: 24h (matches the path-C verdict); 1h reported too.
  * day-block bootstrap 95% CI (10000 resamples).
  * split-half sign consistency, last-20d window, gap3d decorrelated events.

Decision gate (pre-registered, NOT applied automatically here):
  * enforce-block only if 24h excess 95% CI upper < 0 AND split-half same sign
    AND >=30 decorrelated events; otherwise keep path A / keep observing.

Read-only; never touches live code or places orders.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import random
from collections import defaultdict

from arm_edge_eval import load  # reuse the exact join + summarize helpers


def _day(ts_ms: int) -> str:
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000).date().isoformat()


def summarize(pairs, *, seed=7, n_boot=10000):
    """Day-block bootstrap 95% CI over (value, ts) pairs."""
    pairs = [(v, t) for v, t in pairs if v is not None and math.isfinite(v)]
    if not pairs:
        return {"n": 0}
    byday = defaultdict(list)
    for v, t in pairs:
        byday[_day(t)].append(v)
    dm = [sum(d) / len(d) for d in byday.values()]
    mean = sum(v for v, _ in pairs) / len(pairs)
    med = sorted(v for v, _ in pairs)[len(pairs) // 2]
    lo = hi = None
    if len(dm) >= 3:
        rnd = random.Random(seed)
        b = []
        for _ in range(n_boot):
            s = rnd.choices(dm, k=len(dm))
            b.append(sum(s) / len(s))
        b.sort()
        lo, hi = b[int(.025 * len(b))], b[int(.975 * len(b))]
    return {
        "n": len(pairs), "days": len(byday),
        "mean_pct": round(mean, 4), "median_pct": round(med, 4),
        "ci95_pct": [round(lo, 4), round(hi, 4)] if lo is not None else None,
        "win_rate_pct": round(100 * sum(1 for v, _ in pairs if v > 0) / len(pairs), 1),
    }


def gap3d_decorrelate(rows):
    """Keep only the first signal per coin within any 3-day window (independent
    events). rows = [(feat, outcome)] sorted by ts."""
    last = {}
    out = []
    for f, o in rows:
        c = f["coin"]
        t = f["ts"]
        if c not in last or t - last[c] >= 3 * 86_400_000:
            out.append((f, o))
            last[c] = t
    return out


def split_half_sign(rows, key):
    rows = sorted(rows, key=lambda fo: fo[0]["ts"])
    h = len(rows) // 2
    if h == 0:
        return None
    a = [o[key] for _, o in rows[:h] if o.get(key) is not None]
    b = [o[key] for _, o in rows[h:] if o.get(key) is not None]
    if not a or not b:
        return None
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    return {"first_half_mean": round(ma, 4), "second_half_mean": round(mb, 4),
            "same_sign": (ma > 0) == (mb > 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default="/data/decision_shadow_replay_bn90d.jsonl")
    ap.add_argument("--horizons", default="1h,24h")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    report = {"question": "enforce-block path A (fresh & slow>=1)?",
              "primary_metric": "excess19_pct", "primary_horizon": "24h",
              "horizons": {}}

    for h in a.horizons.split(","):
        h = h.strip()
        outcome = f"/data/bn90_outcomes_{h}.jsonl"
        rows = load(a.replay, outcome)  # [(feat, outcome)] joined on (coin,ts)
        # path A = fresh & slow>=1 (matches reconcile classify_path A_fresh_slow)
        A = [(f, o) for f, o in rows
             if f.get("fresh_impulse") and int(f.get("slow_burn_count") or 0) >= 1
             and o.get("covered") and o.get("net19_pct") is not None]
        A.sort(key=lambda fo: fo[0]["ts"])

        indep = gap3d_decorrelate(A)
        # last 20 days window
        if A:
            tmax = max(f["ts"] for f, _ in A)
            last20 = [(f, o) for f, o in A if f["ts"] >= tmax - 20 * 86_400_000]
        else:
            last20 = []

        report["horizons"][h] = {
            "n_pathA": len(A),
            "net19": summarize([(o["net19_pct"], f["ts"]) for f, o in A]),
            "excess19": summarize([(o["excess19_pct"], f["ts"]) for f, o in A]),
            "excess19_split_half": split_half_sign(A, "excess19_pct"),
            "gap3d_independent_events": len(indep),
            "gap3d_excess19": summarize([(o["excess19_pct"], f["ts"]) for f, o in indep]),
            "last20d_excess19": summarize([(o["excess19_pct"], f["ts"]) for f, o in last20]),
        }

    # pre-registered verdict on the PRIMARY horizon/metric
    p = report["horizons"].get("24h", {})
    ci = (p.get("excess19") or {}).get("ci95_pct")
    sh = p.get("excess19_split_half") or {}
    n_indep = p.get("gap3d_independent_events", 0)
    if ci and ci[1] < 0 and sh.get("same_sign") and n_indep >= 30:
        verdict = "ENFORCE-BLOCK path A (24h excess CI<0, split-half consistent, >=30 indep events)"
    elif ci and ci[0] > 0:
        verdict = "KEEP path A (24h excess CI>0)"
    else:
        verdict = "INSUFFICIENT / CI spans 0 — keep path A, keep observing; do NOT enforce"
    report["verdict"] = verdict

    txt = json.dumps(report, ensure_ascii=False, indent=2)
    print(txt)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(txt + "\n")


if __name__ == "__main__":
    main()
