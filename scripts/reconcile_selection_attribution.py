#!/usr/bin/env python3
"""Daily selection-attribution verdict for the ranking/selection arms.

Closes the evidence loop started by ``selection_attribution``: each day it
(1) settles any items whose horizon elapsed, then (2) reads the selected-vs-
deferred forward-return summary and emits a PROMOTE / DEMOTE / HOLD verdict per
arm against the pre-registered gates (n, mean diff, paired p, cost-aware where
applicable).

Safety by design:
  * read-only on the market and on the config — it NEVER flips an arm's mode.
    Promotion still needs an operator to set mode=enforce; this script only
    makes the evidence/verdict visible.
  * verdicts are appended to /data/selection_attribution_verdicts.jsonl (the
    daily report / operator can consume them).
  * best-effort: a verdict day with insufficient/missing data is HOLD.

Usage:
    python3 scripts/reconcile_selection_attribution.py            # dry-run
    python3 scripts/reconcile_selection_attribution.py --write    # persist
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict

from hermes_trader.agents import selection_attribution as sa
from hermes_trader.loop_runtime import data_dir

# Pre-registered promotion gates per source (matches the v2 plan §4).
GATES: Dict[str, Dict[str, Any]] = {
    "signal_ranking": {"min_pairs": 30, "min_diff_pct": 0.0, "max_p": 0.05},
    "coin_selection": {"min_pairs": 30, "min_diff_pct": 0.0, "max_p": 0.05},
}


def _verdict_for(source: str) -> Dict[str, Any]:
    gate = GATES.get(source)
    if gate is None:
        return {"source": source, "verdict": "HOLD", "reason": "no gate"}
    summ = sa.attribution_summary(source)

    n_pair = int(summ.get("n_pairs") or 0)
    diff = summ.get("mean_diff_pct")
    p = summ.get("p_value")

    # DEMOTE first (asymmetric: cheap to stop). No demonstrable edge -> stop.
    if diff is not None and diff <= gate["min_diff_pct"]:
        return {"source": source, "verdict": "DEMOTE",
                "reason": f"mean_diff {diff} <= {gate['min_diff_pct']}",
                "summary": summ}

    # PROMOTE needs every gate to clear.
    enough = (n_pair >= gate["min_pairs"])
    edge = (diff is not None and diff > gate["min_diff_pct"])
    sig = (p is not None and p < gate["max_p"])
    if enough and edge and sig:
        return {"source": source, "verdict": "PROMOTE",
                "reason": (f"n_pairs={n_pair}, diff={diff}, p={p}; "
                           f"operator may set mode=enforce (no size up)"),
                "summary": summ}

    return {"source": source, "verdict": "HOLD",
            "reason": (f"gates not yet met (n_pairs={n_pair}/"
                       f"{gate['min_pairs']}, diff={diff}, p={p})"),
            "summary": summ}


def _unstucking_verdict() -> Dict[str, Any]:
    """B3: after the 7-day window, close the arm if evidence is absent."""
    path = os.path.join(data_dir(), "unstucking.jsonl")
    n = 0
    n_nonempty = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                n += 1
                if rec.get("selected"):
                    n_nonempty += 1
    except FileNotFoundError:
        pass
    # The 7-day checkpoint is enforced by the scheduler only running this
    # script from 2026-10-16 onward; before that we always report HOLD.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today < "2026-10-16":
        return {"source": "unstucking", "verdict": "HOLD",
                "reason": f"7-day checkpoint is 2026-10-16 (records={n})"}
    if n_nonempty < 5:
        return {"source": "unstucking", "verdict": "DEMOTE",
                "reason": f"non-empty evaluations {n_nonempty} < 5"}
    return {"source": "unstucking", "verdict": "HOLD",
            "reason": f"{n_nonempty} non-empty evaluations; needs n>=15 to promote"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="persist verdicts into the verdicts JSONL")
    args = ap.parse_args(argv)

    # Settle mature items first (best-effort).
    settled = sa.settle_due()

    verdicts = [_verdict_for(src) for src in GATES]
    verdicts.append(_unstucking_verdict())

    payload = {"ts": int(time.time() * 1000), "settled": settled,
               "verdicts": verdicts}

    if args.write:
        out = os.path.join(data_dir(), "selection_attribution_verdicts.jsonl")
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    print("=== selection attribution verdict ===")
    print(f"settled this run: {settled}")
    for v in verdicts:
        print(f"  {v['source']:16s} {v['verdict']:7s} — {v['reason']}")
    if not args.write:
        print("(dry-run; pass --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
