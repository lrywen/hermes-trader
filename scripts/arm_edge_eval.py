#!/usr/bin/env python3
"""M2 arm edge evaluator (offline, SHADOW).

Joins the reconstructed structural feature vectors in the 88-day Binance
replay (decision_shadow_replay_bn90d.jsonl) with forward-return outcomes
(bn90_outcomes_<h>.jsonl, net of 19bp) on (coin, ts), then evaluates a set of
CANDIDATE CONSTRUCTION FIXES as pure predicates over the feature vector.

An arm is a function(feature) -> one of:
    "admit"   : the arm would still enter at this decision point
    "block"   : the arm would refuse (and thus avoids the loss / forgoes gain)
For each arm we report, over the rows where the LIVE gate admitted, what
happens if we apply the arm:
  * kept    = rows the arm still admits (their net P&L retained)
  * removed = rows the arm blocks (loss avoided OR gain forgone)
The decision metric is net P&L of the resulting portfolio and edge vs the
blocked baseline, with day-block bootstrap 95% CI. Arms do NOT touch live code.
"""
import argparse
import datetime
import json
import math
import random
from collections import defaultdict

COST19 = 0.0019


def summarize(pairs):
    pairs = [(v, t) for v, t in pairs if v is not None and math.isfinite(v)]
    if not pairs:
        return {"n": 0}
    byday = defaultdict(list)
    for v, t in pairs:
        byday[datetime.datetime.utcfromtimestamp(t / 1000).date().isoformat()].append(v)
    dm = [sum(d) / len(d) for d in byday.values()]
    mean = sum(v for v, _ in pairs) / len(pairs)
    lo = hi = None
    if len(dm) >= 3:
        rnd = random.Random(7)
        b = []
        for _ in range(3000):
            s = rnd.choices(dm, k=len(dm))
            b.append(sum(s) / len(s))
        b.sort()
        lo, hi = b[int(.025 * len(b))], b[int(.975 * len(b))]
    return {"n": len(pairs), "days": len(byday), "mean_pct": round(mean, 4),
            "ci95_pct": [round(lo, 4), round(hi, 4)] if lo is not None else None}


# ── ARMS: pure predicates over an admitted feature dict; True = REMOVE/block ──
def arm_remove_daily_mover(f):
    """R1: block the daily-mover-only bypass (no fresh impulse)."""
    return bool(f.get("dMover")) and not bool(f.get("fresh_impulse"))

def arm_require_fresh(f):
    """R1b: every long must have a fresh impulse (kills mover-only + trend-only)."""
    return not bool(f.get("fresh_impulse"))

def arm_fresh_or_strong_slow(f, k=3):
    """R1c: fresh impulse OR a deep slow-burn (>=k) — allow only backed movers."""
    return not (bool(f.get("fresh_impulse")) or int(f.get("slow_burn_count") or 0) >= k)

def arm_score_floor40(f):
    """R-score: require composite>=40 (instead of the near-dead 45 OR carriers)."""
    return float(f.get("composite_score") or 0) < 40.0

def arm_score_floor50(f):
    return float(f.get("composite_score") or 0) < 50.0


# ── Stage-4 (2026-10-30): extend to the slow / conf / score-band dimensions ──
# The original 5 arms all live on the path axis (fresh/dMover). The three-way
# review converged on two real axes (A: fresh+brk+slow, C: dMover+not-brk), so
# brk is NOT re-tested (it is the path axis). These arms probe the remaining
# un-sliced dimensions: slow_burn_count depth, confidence band, score band.
def arm_slow_ge2(f):
    """Block deep slow-burn (slow_burn_count>=2) — stage-3 lead, needs CI."""
    return int(f.get("slow_burn_count") or 0) >= 2

def arm_slow_eq1(f):
    """Block shallow slow-burn (slow_burn_count==1) — keep only deep structure."""
    return int(f.get("slow_burn_count") or 0) == 1

def arm_score_mid_20_30(f):
    """Block the mid-score band 20-30 (stage-3 negative sub-cluster)."""
    s = float(f.get("composite_score") or 0)
    return 20 <= s < 30

def arm_score_lt20(f):
    """Block low-score (<20) — keep only scored entries."""
    return float(f.get("composite_score") or 0) < 20

def arm_conf_lt70(f):
    """Block low-confidence (<0.70) — note: replay conf is neutralized to 0.9,
    so this arm is a no-op on replay data and only meaningful on live verdicts."""
    return float(f.get("confidence") or 0) < 0.70

def arm_no_vol(f):
    """Block entries without a 2-sigma volumeSpike (vol=False)."""
    return not bool(f.get("vol"))


ARMS = {
    "R1_remove_daily_mover": arm_remove_daily_mover,
    "R1b_require_fresh_impulse": arm_require_fresh,
    "R1c_fresh_or_slow>=3": arm_fresh_or_strong_slow,
    "Rscore_floor40": arm_score_floor40,
    "Rscore_floor50": arm_score_floor50,
    # new dimensions
    "S_slow_ge2": arm_slow_ge2,
    "S_slow_eq1": arm_slow_eq1,
    "S_score_mid20_30": arm_score_mid_20_30,
    "S_score_lt20": arm_score_lt20,
    "S_conf_lt70": arm_conf_lt70,
    "S_no_vol": arm_no_vol,
}


def load(replay, outcome):
    feat = {}
    for line in open(replay):
        r = json.loads(line)
        feat[(r["coin"], r["ts"])] = r
    out = {}
    for line in open(outcome):
        r = json.loads(line)
        if r.get("covered"):
            out[(r["coin"], r["ts"])] = r
    rows = []
    for k, f in feat.items():
        if k in out:
            rows.append((f, out[k]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default="/data/decision_shadow_replay_bn90d.jsonl")
    ap.add_argument("--outcome", default="/data/bn90_outcomes_1h.jsonl")
    ap.add_argument("--arms", default=",".join(ARMS))
    a = ap.parse_args()
    rows = load(a.replay, a.outcome)
    admitted = [(f, o) for f, o in rows if f.get("admitted")]
    blocked = [(f, o) for f, o in rows if not f.get("admitted")]
    base_blocked = [(o["net19_pct"], f["ts"]) for f, o in blocked]

    report = {
        "outcome_file": a.outcome,
        "n_admitted": len(admitted),
        "n_blocked_baseline": len(blocked),
        "baseline": {
            "live_admitted_net": summarize([(o["net19_pct"], f["ts"]) for f, o in admitted]),
            "blocked_net": summarize(base_blocked),
        },
        "arms": {},
    }
    for name in a.arms.split(","):
        name = name.strip()
        fn = ARMS[name]
        kept_pairs, removed_pairs = [], []
        n_removed = 0
        removed_by_path = defaultdict(int)
        for f, o in admitted:
            p = o["net19_pct"]; t = f["ts"]
            if fn(f):
                removed_pairs.append((p, t)); n_removed += 1
                removed_by_path[o.get("path", "?")] += 1
            else:
                kept_pairs.append((p, t))
        # avoided = mean net of removed rows (positive => arm avoided losses)
        avoided = summarize(removed_pairs)
        kept = summarize(kept_pairs)
        # arm portfolio net = kept only (removed trades not taken)
        report["arms"][name] = {
            "removed": n_removed,
            "removed_share_pct": round(100 * n_removed / max(1, len(admitted)), 1),
            "removed_by_path": dict(removed_by_path),
            "mean_removed_net_pct": avoided.get("mean_pct"),
            "removed_ci95": avoided.get("ci95_pct"),
            "kept_portfolio": kept,
            "kept_minus_blocked_pct": round(
                (kept.get("mean_pct", 0) - summarize(base_blocked).get("mean_pct", 0)), 4),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
