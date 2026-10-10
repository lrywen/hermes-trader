#!/usr/bin/env python3
"""R4 orchestration: prepare (never force) canary promotion requests.

Each run gathers the multi-dimensional evidence for the registered shadow
arms and, for any arm that clears every pre-registered gate, queues a
PENDING_APPROVAL record via :mod:`canary_pipeline`. It is strictly
fail-closed and read-only on the market/config:

  * never flips ``mode`` to LIVE and never mints ``live_acceptance_gate.json``;
  * an operator reviews the pending record and decides separately (the human
    keeps the final one-vote veto);
  * arms lacking evidence are simply skipped (an evidence gap is not promoted).

Usage:
    python3 scripts/prepare_canary_promotion.py             # dry-run
    python3 scripts/prepare_canary_promotion.py --write     # queue pending rows
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Allow running both from the repo root and inside the container image.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_trader.agents import canary_pipeline as cp
from hermes_trader.agents import live_gate

# Arms eligible to be prepared for a canary promotion. Kept explicit: only
# arms whose counterfactual evidence is actually collected/graded are listed.
CANDIDATE_ARMS = (
    "signal_age_decay",
    "trend_filter_200ma",
    "pullback",
    "ta_late_entry",
    "xs_reversal",
)


def gather_evidence(arm: str, grades: dict, *, killswitch_tested: bool,
                    orderable: bool) -> dict:
    """Map a shadow-grade snapshot to the canary evidence contract.

    Statistical robustness (WFA/DSR/PBO) is read from the grade's optional
    ``stats`` block when R5 has populated it; conservative net edge uses the
    hit-set counterfactual PnL. Dimensions the grade cannot supply yet are
    left absent so their gate fails closed rather than being guessed.
    """
    g = grades.get(arm) or {}
    stats = g.get("r5_stats") or {}
    # The slim grade snapshot stores ``windows`` as a list of per-window stats
    # and mirrors ``mature_outcomes`` at the top level. Use the top-level
    # value; fall back to the widest window's count when not mirrored.
    windows = g.get("windows")
    mature = g.get("mature_outcomes")
    if mature is None and isinstance(windows, list) and windows:
        widest = max(windows, key=lambda w: w.get("window_h", 0))
        mature = widest.get("mature_outcomes")
    ev: dict = {
        "mature_outcomes": mature,
        "independent_days": g.get("independent_days"),
        "net_edge_bps": stats.get("net_edge_bps"),
        "bb_ci_lo_bps": stats.get("bb_ci_lo_bps"),
        "dsr_p": stats.get("dsr_p"),
        "pbo": stats.get("pbo"),
        "wfr": stats.get("wfr"),
        "realistic_fill_positive": g.get("realistic_fill_positive"),
        "killswitch_tested": killswitch_tested,
        "orderable": orderable,
    }
    return ev


def _load_grades() -> dict:
    """Best-effort: read the latest shadow-grade snapshot keyed by arm.

    Returns an empty map when the grader has not run, in which case every
    candidate is skipped (fail closed). No hard import dependency on the
    grader internals keeps this script runnable standalone.
    """
    path = os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"),
                        "shadow_grade_history.jsonl")
    last: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for arm in rec.get("arms", []) if isinstance(rec, dict) else []:
                    if isinstance(arm, dict) and arm.get("arm"):
                        last[arm["arm"]] = arm
    except FileNotFoundError:
        return {}
    return last


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="queue PENDING_APPROVAL records that clear all gates")
    ap.add_argument("--arms", default="",
                    help="comma override of the candidate arm list")
    ap.add_argument("--killswitch-tested", action="store_true",
                    help="assert the kill-switch has been live-tested (R6)")
    ap.add_argument("--orderable", action="store_true",
                    help="assert the account meets min-order/target size")
    args = ap.parse_args(argv)

    arms = (tuple(a.strip() for a in args.arms.split(",") if a.strip())
            if args.arms else CANDIDATE_ARMS)
    grades = _load_grades()

    cfg_sha = live_gate.config_sha256(
        {"mode": "SHADOW", "ts": int(time.time())})

    queued, skipped = [], []
    for arm in arms:
        ev = gather_evidence(
            arm, grades,
            killswitch_tested=args.killswitch_tested,
            orderable=args.orderable)
        res = cp.evaluate_gates(ev)
        if not res["passed"]:
            failed = [c["gate"] for c in res["checks"] if not c["passed"]]
            skipped.append({"arm": arm, "blocked_on": failed})
            continue
        try:
            rec = cp.build_pending_record(
                arm, ev, config_sha256=cfg_sha)
        except ValueError:
            skipped.append({"arm": arm, "blocked_on": ["build"]})
            continue
        if args.write:
            cp.append_record(rec)
        queued.append(arm)

    print("=== canary promotion preparation ===")
    print(f"qualified: {queued if queued else '(none)'}")
    for s in skipped:
        print(f"  skip {s['arm']:20s} blocked_on={s['blocked_on']}")
    if not args.write:
        print("(dry-run; pass --write to queue PENDING_APPROVAL records)")
    else:
        print("pending records await explicit operator approval; no LIVE "
              "change was made")
    return 0


if __name__ == "__main__":
    sys.exit(main())
