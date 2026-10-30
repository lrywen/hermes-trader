#!/usr/bin/env python3
"""Stage-3 (2026-10-30): decompose path A's fresh_impulse by leg/quality.

Stage-2 showed path A (fresh & slow>=1) has no robust carrier alpha
(24h excess mean +0.11% but median -0.46%, CI spans 0). The next question is
WHICH part of the OR-defined fresh_impulse drives the right tail vs the bleed.

Reality of the offline replay (honest limitation): replay_gate reconstructs
``breakout_onset_ms`` only on the bar where breakout fires (win5[-1].t), so the
``recent_brk`` wall-clock window (44% of LIVE shadow path A) is NOT
reconstructed offline. Over the 88-day Binance replay path A therefore splits
as brk=789 (99.5%) / vol_burst=4 — recent_brk and burst_score have no offline
samples. Stage-3 thus decomposes the reconstructable ``brk`` leg by
OBSERVABLE quality features, and explicitly flags recent_brk as an unmeasured
leg that must be judged from live shadow accrual only.

Pre-registered slices over the brk leg (feature predicates only, no tuning):
  * with vs without a 2-sigma volumeSpike (``vol``) — breakout participation
  * slow_burn_count depth: 1 vs >=2
  * composite_score band: <20 / 20-30 / >=30
  * fired-trigger confluence count: 1 / 2 / >=3
Each slice reports 1h & 24h net19/excess19 with day-block bootstrap 95% CI,
n, days, median, win-rate (reuses path_a_forward.summarize).

Read-only; never touches live code or places orders.
"""
from __future__ import annotations

import argparse
import json

from arm_edge_eval import load
from path_a_forward import gap3d_decorrelate, summarize  # reuse stats

SLICES = {
    # name -> predicate(feat)
    "brk_vol_yes": lambda f: bool(f.get("vol")),
    "brk_vol_no": lambda f: not bool(f.get("vol")),
    "slow1": lambda f: int(f.get("slow_burn_count") or 0) == 1,
    "slow_ge2": lambda f: int(f.get("slow_burn_count") or 0) >= 2,
    "score_lt20": lambda f: float(f.get("composite_score") or 0) < 20,
    "score_20_30": lambda f: 20 <= float(f.get("composite_score") or 0) < 30,
    "score_ge30": lambda f: float(f.get("composite_score") or 0) >= 30,
    "conf1": lambda f: len(f.get("fired_names") or []) == 1,
    "conf2": lambda f: len(f.get("fired_names") or []) == 2,
    "conf_ge3": lambda f: len(f.get("fired_names") or []) >= 3,
}


def stat_rows(rows, key):
    return [(o[key], f["ts"]) for f, o in rows if o.get(key) is not None]


def _stat_safe(pairs):
    s = summarize(pairs)
    if s.get("n", 0) == 0:
        return {"n": 0, "note": "no samples"}
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default="/data/decision_shadow_replay_bn90d.jsonl")
    ap.add_argument("--horizons", default="1h,24h")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    report = {
        "question": "which part of the path-A brk leg has/leaks edge?",
        "limitation": ("replay reconstructs only same-bar breakout (brk); "
                       "recent_brk (44% of live path A) and burst_score have "
                       "NO offline samples — judge them from live shadow only."),
        "slices": {},
    }

    horizon_rows = {}
    for h in a.horizons.split(","):
        h = h.strip()
        rows = load(a.replay, f"/data/bn90_outcomes_{h}.jsonl")
        brk = [(f, o) for f, o in rows
               if f.get("fresh_src") == "brk"
               and int(f.get("slow_burn_count") or 0) >= 1
               and o.get("covered") and o.get("net19_pct") is not None]
        brk.sort(key=lambda fo: fo[0]["ts"])
        horizon_rows[h] = brk

    for name, pred in SLICES.items():
        report["slices"][name] = {}
        for h, brk in horizon_rows.items():
            sub = [(f, o) for f, o in brk if pred(f)]
            indep = gap3d_decorrelate(sub)
            report["slices"][name][h] = {
                "n": len(sub),
                "net19": _stat_safe(stat_rows(sub, "net19_pct")),
                "excess19": _stat_safe(stat_rows(sub, "excess19_pct")),
                "gap3d_n": len(indep),
                "gap3d_excess19": _stat_safe(stat_rows(indep, "excess19_pct")),
            }

    txt = json.dumps(report, ensure_ascii=False, indent=2)
    print(txt)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(txt + "\n")


if __name__ == "__main__":
    main()
