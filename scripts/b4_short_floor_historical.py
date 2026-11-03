#!/usr/bin/env python3
"""B4 (2026-11-03): does the $50M short 24h-volume floor actually protect?

runner_entry_gate.min_short_volume_usd=50e6 blocks shorts on coins with
24h quote volume < $50M. The live short_floor_would_block shadow field has
only ~105 rows (d<30, insufficient). The user prefers reusing historical data,
and the runner-gate shadow rows already carry the candidate-time
volume_24h_usd for 1597 short candidates. Although every short is currently
blocked upstream (allow_shorts=false / scan_long_only), their FORWARD returns
are a valid counterfactual: split short candidates by candidate-time volume
(<50M floor-blocked vs >=50M floor-passed) and compare P0-3-corrected forward
net_bp19. If the <50M group is materially worse, the floor has evidence; if
indistinguishable or better, the floor is unsupported.

Day-block bootstrap (independent UTC days) is the inference unit; rows missing
volume_24h_usd are reported separately, never silently pooled.

Read-only; writes nothing under /data.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics as st
from collections import defaultdict
from typing import Any, Optional

_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
FLOOR = 50e6
COST = 0.0019  # round-trip taker, matches enrich net_bp19 convention


def _load(pattern: str) -> list[dict[str, Any]]:
    out = []
    for f in sorted(glob.glob(pattern)):
        for line in open(f):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _signed_net(o: dict[str, Any], hz: str) -> Optional[float]:
    blk = o.get("net_bp19")
    if not isinstance(blk, dict):
        return None
    v = blk.get(hz)
    if v is None:
        return None
    if str(o.get("side")).lower() == "short":
        return -v - 2 * COST
    return v


def _stat(xs: list[float], days: list[int], B: int = 4000, seed: int = 23):
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None,
                "ci95_bp": [None, None], "win_pct": None}
    byd = defaultdict(list)
    for x, d in zip(xs, days):
        byd[d].append(x)
    blocks = [st.mean(v) for v in byd.values()]
    k = len(blocks)
    rnd = random.Random(seed)
    means = sorted(st.mean(rnd.choice(blocks) for _ in range(k)) for _ in range(B))
    return {"n": len(xs), "days": k, "mean_bp": round(1e4 * st.mean(xs), 2),
            "ci95_bp": [round(1e4 * means[int(.025 * B)], 2),
                        round(1e4 * means[int(.975 * B)], 2)],
            "win_pct": round(100 * sum(1 for x in xs if x > 0) / len(xs), 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--horizon", default="1h")
    ap.add_argument("--floor", type=float, default=FLOOR)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    hz = args.horizon

    rows = [r for r in _load(os.path.join(args.data_dir, "runner_gate_shadow.jsonl*"))
            if not r.get("replay") and r.get("side") == "short"]
    outcomes = {}
    for line in open(os.path.join(args.data_dir, "decision_shadow_outcomes.jsonl")):
        try:
            o = json.loads(line)
        except Exception:
            continue
        outcomes[(o.get("ts"), o.get("coin"), o.get("side"))] = o

    groups = {"lt_floor": [], "ge_floor": [], "missing_vol": []}
    gdays = {"lt_floor": [], "ge_floor": [], "missing_vol": []}
    tier_xs: dict[str, list[float]] = defaultdict(list)
    tier_d: dict[str, list[int]] = defaultdict(list)

    for r in rows:
        o = outcomes.get((r.get("ts"), r.get("coin"), r.get("side")))
        if not o or not o.get("covered"):
            continue
        v = _signed_net(o, hz)
        if v is None:
            continue
        d = int(r["ts"]) // 86_400_000
        vol = r.get("volume_24h_usd")
        if vol is None:
            g = "missing_vol"
        elif float(vol) < args.floor:
            g = "lt_floor"
        else:
            g = "ge_floor"
        groups[g].append(v)
        gdays[g].append(d)
        tier = r.get("volume_tier") or "unknown"
        tier_xs[tier].append(v)
        tier_d[tier].append(d)

    # DIFF in day-block means (floor-passed minus floor-blocked)
    def daymeans(g):
        byd = defaultdict(list)
        for x, d in zip(groups[g], gdays[g]):
            byd[d].append(x)
        return {d: st.mean(v) for d, v in byd.items()}
    a, b = daymeans("ge_floor"), daymeans("lt_floor")
    common_days = sorted(set(a) & set(b))
    diffs = [a[d] - b[d] for d in common_days]
    if len(diffs) >= 2:
        rnd = random.Random(71)
        bs = sorted(st.mean(rnd.choice(diffs) for _ in range(len(diffs))) for _ in range(4000))
        diff_stat = {"common_days": len(diffs),
                     "ge_minus_lt_mean_bp": round(1e4 * st.mean(diffs), 2),
                     "ci95_bp": [round(1e4 * bs[100], 2), round(1e4 * bs[3899], 2)]}
    else:
        diff_stat = {"common_days": len(diffs), "ge_minus_lt_mean_bp": None,
                     "ci95_bp": [None, None]}

    report = {
        "meta": {"horizon": hz, "short_rows": len(rows), "floor_usd": args.floor,
                 "short_sign": "P0-3-corrected",
                 "interpretation": "lt_floor = candidates the $50M floor would "
                                   "block. If lt_floor mean << ge_floor (and the "
                                   "ge-minus-lt DIFF CI is >0), the floor protects."},
        "groups": {g: _stat(groups[g], gdays[g]) for g in groups},
        "by_volume_tier": {t: _stat(tier_xs[t], tier_d[t]) for t in sorted(tier_xs)},
        "diff_ge_minus_lt": diff_stat,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
