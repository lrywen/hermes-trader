#!/usr/bin/env python3
"""Audit effective trigger config vs canonical literals (M0, plan G0).

Why: get_config() returns CANONICAL defaults; the live scanner resolves via
trigger_thresholds_params()/trigger_weights_params() (env > /data file >
canonical). A prior audit misread momentumBurst as 3bar/3% by using get_config
while production actually runs 2bar/4% (a deploy-lag divergence). This script
makes canonical<->effective drift on trigger leaves explicit and fails unless
every drift is value-bound-registered in _REVIEWED_TRIGGER_DIVERGENCES.

Exit codes: 0 = no unregistered divergence; 1 = unregistered divergence found;
2 = resolution error. Read-only: never mutates config.
"""
from __future__ import annotations

import argparse
import json
import sys

from hermes_trader.agents.config import (
    TRIGGER_CONFIG,
    _TRIGGER_THRESHOLDS_KEYMAP,
    _TRIGGER_WEIGHTS_KEYMAP,
    trigger_thresholds_params,
    trigger_weights_params,
)

# Value-bound registry of KNOWN, REVIEWED canonical<->effective trigger drifts.
# leaf -> (canonical, effective). Any change on either side re-reviews red.
# 2026-10-29 M0: momentumBurst canonical was deliberately relaxed to 3bar/3%
# (comment: "2bar/4% fired only 9x ... near-dead") but the live /data file still
# pins 2bar/4%; production therefore runs the near-dormant variant. Kept as an
# explicit sanctioned divergence until M2 produces edge evidence for either.
_REVIEWED_TRIGGER_DIVERGENCES: dict[str, tuple[object, object]] = {
    "thresholds.momentumLookback": (3, 2),
    "thresholds.momentumPct": (3.0, 4.0),
}


def _audit() -> tuple[list[dict], list[dict]]:
    eff_th = trigger_thresholds_params()
    eff_w = trigger_weights_params()
    unregistered: list[dict] = []
    reviewed: list[dict] = []

    canon_w = TRIGGER_CONFIG["weights"]
    for _leaf, camel in _TRIGGER_WEIGHTS_KEYMAP.items():
        c = canon_w[camel]
        e = eff_w.get(camel)
        if c != e:
            rec = {"leaf": f"weights.{camel}", "canonical": c, "effective": e}
            key = f"weights.{camel}"
            (reviewed if _REVIEWED_TRIGGER_DIVERGENCES.get(key) == (c, e)
             else unregistered).append(rec)

    for _leaf, (camel, _is_int) in _TRIGGER_THRESHOLDS_KEYMAP.items():
        c = TRIGGER_CONFIG["thresholds"].get(camel)
        e = eff_th.get(camel)
        # compare numerically where both are numbers
        same = (c == e) or (
            isinstance(c, (int, float)) and isinstance(e, (int, float)) and float(c) == float(e)
        )
        if not same:
            rec = {"leaf": f"thresholds.{camel}", "canonical": c, "effective": e}
            key = f"thresholds.{camel}"
            reg = _REVIEWED_TRIGGER_DIVERGENCES.get(key)
            match = reg is not None and (
                reg == (c, e) or (
                    isinstance(reg[0], (int, float)) and isinstance(reg[1], (int, float))
                    and isinstance(c, (int, float)) and isinstance(e, (int, float))
                    and float(reg[0]) == float(c) and float(reg[1]) == float(e)
                )
            )
            (reviewed if match else unregistered).append(rec)
    return unregistered, reviewed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    try:
        unregistered, reviewed = _audit()
    except Exception as e:  # resolution failure must be loud, never silent-green
        print(f"effective-config audit ERROR: {e!r}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"unregistered": unregistered, "reviewed": reviewed},
                         ensure_ascii=False, indent=2))
    else:
        print("== reviewed (sanctioned, value-bound) ==")
        for r in reviewed:
            print(f"  {r['leaf']:34s} canonical={r['canonical']!r:>8} effective={r['effective']!r}")
        print("== unregistered trigger divergences ==")
        if not unregistered:
            print("  none")
        for r in unregistered:
            print(f"  {r['leaf']:34s} canonical={r['canonical']!r:>8} effective={r['effective']!r}")

    if unregistered:
        print(
            f"\nFAIL: {len(unregistered)} unregistered canonical<->effective "
            "trigger divergence(s). Review and either align live/config or add a "
            "value-bound entry to _REVIEWED_TRIGGER_DIVERGENCES.",
            file=sys.stderr,
        )
        return 1
    print("OK: effective trigger config matches canonical or is reviewed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
