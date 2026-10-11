#!/usr/bin/env python3
"""Reconcile weight_shadow SHADOW verdicts (audit 2026-10-11).

Reads ~/.hermes-trading/weight_shadow.jsonl (override via
HERMES_WEIGHT_SHADOW_FILE). Each row is emitted ONLY when the canonical
reweight (trendStrength 0.55->0.30, uptrendMomentum 0->0.20, momentumBurst
2bar/4% -> 3bar/3%) flips the composite-gate decision vs the live pinned
weights:

  * would_surface : live dropped the coin (score<gate) but candidate admits it
  * would_block   : live admitted it but candidate drops it

This is a self-contained distribution summary (flip counts, score gap, fired
trigger mix). Forward-PnL grading of the flipped set is a separate step (the
arm first establishes whether a production switch would change any decisions
at all — if flips are ~0 the reweight is behavior-neutral here).

Pure paper reconciliation: never places orders and never writes config.

Usage:
    python3 scripts/reconcile_weight_shadow.py
    python3 scripts/reconcile_weight_shadow.py --window-hours 168
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

SHADOW_FILE = os.environ.get(
    "HERMES_WEIGHT_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/weight_shadow.jsonl"),
)


def _parse_ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=float, default=None)
    ap.add_argument("--file", default=SHADOW_FILE)
    args = ap.parse_args(argv)

    p = Path(args.file)
    if not p.exists():
        print(f"no shadow file at {p}")
        return 0

    cutoff = None
    if args.window_hours is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600

    rows = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("rule") != "weight_shadow":
                continue
            if cutoff is not None and _parse_ts(r["timestamp"]) < cutoff:
                continue
            rows.append(r)

    print(f"weight_shadow decision-flip rows: {len(rows)} ({p.name})")
    if not rows:
        print("  (no flips — the reweight is behavior-neutral for this window, "
              "or the arm is not armed)")
        return 0

    surf = [r for r in rows if r.get("would") == "would_surface"]
    block = [r for r in rows if r.get("would") == "would_block"]
    print(f"  would_surface: {len(surf)} ({len(surf)/len(rows)*100:.0f}%)  "
          f"would_block: {len(block)} ({len(block)/len(rows)*100:.0f}%)")

    gap = [abs(r["detail"]["candidate_score"] - r["detail"]["live_score"])
           for r in rows]
    print(f"  |candidate-live| score gap: mean={statistics.mean(gap):.1f} "
          f"max={max(gap):.1f} gate={rows[0]['detail']['gate']:.0f}")

    trig = collections.Counter()
    for r in rows:
        for t in r["detail"].get("fired_triggers", []):
            trig[t] += 1
    print("  fired-trigger mix on flip rows:",
          ", ".join(f"{k}={v}" for k, v in trig.most_common()))

    # surfaced-by-candidate coins are the ones whose forward PnL must be graded
    # before promoting the reweight; list the most recent few for inspection.
    print("  recent would_surface coins:",
          ", ".join(r["coin"] for r in surf[-10:]) or "(none)")
    print("\nverdict rule: promote the reweight only if would_surface coins "
          "show positive forward expectancy AND would_block coins show "
          "negative forward expectancy (grade with a 12-bar 5m event study).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
