#!/usr/bin/env python3
"""E2: Analyze runner_gate blocked-signal population for further optimization.

Replays session-log scan+execute events and reports:
  1. blocked_by distribution for LONG candidates
  2. trigger composition of admitted vs blocked (new formula)
  3. breakout signals split by structure gate (slow>=1 vs score>=min_score)
  4. co-firing matrix of the three fresh_impulse components
  5. score distribution of admitted/blocked
"""
from __future__ import annotations
import json, os
from collections import Counter, defaultdict

SESSION_LOG = os.environ.get("SESSION_LOG_PATH", "/data/session-log.jsonl")
MIN_SCORE = 30.0


def _to_str(v):
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return str(v) if v is not None else ""


def main():
    latest_scan = {}
    long_events = []
    with open(SESSION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = ev.get("event")
            ts = ev.get("ts") or ev.get("timestamp")
            if et == "scan" and ts:
                for cs in ev.get("coin_scores", []) or []:
                    coin = cs.get("coin")
                    if not coin:
                        continue
                    latest_scan[coin] = {
                        "score": float(cs.get("score", 0) or 0),
                        "triggers": set(cs.get("triggers", []) or []),
                    }
            elif et == "execute" and ts and ev.get("side") == "long":
                coin = ev.get("coin")
                if not coin or coin not in latest_scan:
                    continue
                sc = latest_scan[coin]
                trg = sc["triggers"]
                long_events.append({
                    "coin": coin,
                    "executed": bool(ev.get("executed")),
                    "blocked_by": _to_str(ev.get("blocked_by")),
                    "score": sc["score"],
                    "triggers": trg,
                    "volume": "volumeSpike" in trg,
                    "breakout": "breakout" in trg,
                    "burst": "momentumBurst" in trg,
                    "slow": sum(1 for t in trg if t in (
                        "volumeBuildup1h", "higherLows1h", "trendFlip1h")),
                })

    n = len(long_events)
    print(f"Total LONG execute events with matched scan: {n}\n")

    # 1. blocked_by distribution
    print("=== 1. blocked_by distribution (LONG) ===")
    bb = Counter()
    for e in long_events:
        if e["executed"]:
            bb["(executed)"] += 1
        else:
            reason = e["blocked_by"] or "(none)"
            # collapse to gate name
            gate = reason.split("(")[0].strip() if "(" in reason else reason
            bb[gate] += 1
    for k, v in bb.most_common():
        print(f"  {v:5d}  {k}")
    print()

    # 2. new formula admitted vs blocked
    def is_admitted(e):
        return (e["breakout"] or (e["volume"] and e["burst"])
                or (e["burst"] and e["score"] >= MIN_SCORE))

    admitted = [e for e in long_events if is_admitted(e)]
    blocked = [e for e in long_events if not is_admitted(e)]
    print(f"=== 2. New fresh_impulse formula ===")
    print(f"  fresh_impulse TRUE:  {len(admitted)} ({len(admitted)/n*100:.1f}%)")
    print(f"  fresh_impulse FALSE: {len(blocked)} ({len(blocked)/n*100:.1f}%)\n")

    print("  Trigger composition — fresh_impulse FALSE signals:")
    comp = Counter()
    for e in blocked:
        key = (f"vol={int(e['volume'])}", f"brk={int(e['breakout'])}",
               f"burst={int(e['burst'])}")
        comp[" ".join(key)] += 1
    for k, v in comp.most_common():
        print(f"    {v:5d}  {k}")
    print()

    # 3. structure gate analysis on fresh_impulse TRUE
    print("=== 3. Structure gate on fresh_impulse TRUE (long) ===")
    print("  structured_runner requires: slow>=1 OR score>=30")
    has_struct = [e for e in admitted if e["slow"] >= 1 or e["score"] >= MIN_SCORE]
    no_struct = [e for e in admitted if not (e["slow"] >= 1 or e["score"] >= MIN_SCORE)]
    print(f"  passes structure: {len(has_struct)}")
    print(f"  fails structure:  {len(no_struct)}")
    if no_struct:
        print("  fail-by-coin (score, slow, triggers):")
        for e in no_struct[:25]:
            print(f"    {e['coin']:<12} score={e['score']:5.1f} slow={e['slow']} "
                  f"triggers={sorted(e['triggers'])}")
    print()

    # 4. co-firing matrix for the 3 components
    print("=== 4. fresh_impulse component co-firing (all LONG events) ===")
    mat = Counter()
    for e in long_events:
        key = (e["volume"], e["breakout"], e["burst"])
        mat[key] += 1
    labels = ["volume", "breakout", "burst"]
    print(f"  {'vol':>4} {'brk':>4} {'burst':>5}  {'count':>6}")
    for key in sorted(mat, key=lambda k: -mat[k]):
        print(f"  {int(key[0]):>4} {int(key[1]):>4} {int(key[2]):>5}  "
              f"{mat[key]:>6}")
    print()

    # 5. score distribution
    print("=== 5. Score distribution ===")
    for label, group in [("fresh=TRUE", admitted), ("fresh=FALSE", blocked)]:
        scores = sorted(e["score"] for e in group)
        if not scores:
            continue
        pct = lambda p: scores[min(len(scores)-1, int(len(scores)*p))]
        print(f"  {label}: n={len(scores)} min={scores[0]:.0f} "
              f"p25={pct(.25):.0f} med={pct(.5):.0f} p75={pct(.75):.0f} "
              f"max={scores[-1]:.0f}")
    print()

    # 6. Among fresh=TRUE, executed vs still-blocked (other gates)
    print("=== 6. fresh_impulse TRUE: executed vs blocked by OTHER gates ===")
    ex = [e for e in admitted if e["executed"]]
    bl = [e for e in admitted if not e["executed"]]
    print(f"  executed: {len(ex)}")
    print(f"  blocked by other gates: {len(bl)}")
    other_bb = Counter()
    for e in bl:
        reason = e["blocked_by"] or "(none)"
        gate = reason.split("(")[0].strip() if "(" in reason else reason
        other_bb[gate] += 1
    for k, v in other_bb.most_common(10):
        print(f"    {v:5d}  {k}")
    print()

    # 7. breakout-only subpopulation: slow vs score split
    print("=== 7. breakout-only (no volume/burst): structure breakdown ===")
    bo = [e for e in long_events if e["breakout"] and not e["volume"] and not e["burst"]]
    bo_struct = [e for e in bo if e["slow"] >= 1 or e["score"] >= MIN_SCORE]
    bo_nostruct = [e for e in bo if not (e["slow"] >= 1 or e["score"] >= MIN_SCORE)]
    print(f"  total breakout-only: {len(bo)}")
    print(f"  with structure: {len(bo_struct)} | without: {len(bo_nostruct)}")
    # score bands
    bands = Counter()
    for e in bo:
        s = e["score"]
        if s < 15: bands["<15"] += 1
        elif s < 25: bands["15-25"] += 1
        elif s < 30: bands["25-30"] += 1
        else: bands[">=30"] += 1
    print("  score bands:")
    for k in ["<15", "15-25", "25-30", ">=30"]:
        print(f"    {k:>6}: {bands[k]}")


if __name__ == "__main__":
    main()
