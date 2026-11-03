#!/usr/bin/env python3
"""C7 (2026-11-03): replay-vs-live runner-gate equivalence audit.

P4 moved the order-lifecycle cluster into execution/order_lifecycle.py as a
verbatim move with wrapper forwarding; the gate decision itself stayed in
executor. The open question ("复核待取证项") is whether the offline gate REPLAY
(scripts/replay_gate.py, which calls the SAME _runner_entry_block_reason_impl
over reconstructed candles) agrees with the LIVE verdicts over their overlap
window (live rows exist from 2026-10-24). replay_gate.py itself states its
output "must be calibrated against the real 2026-10-24+ subset before use" —
that calibration had never been done.

This script performs it, read-only:

  * aligns replay rows and LIVE long rows by (coin, 5m bar ts);
  * compares admitted and the structural block reason;
  * classifies every disagreement by whether it is EXPECTED from the replay's
    declared neutralization (AI confidence; replay forces conf=pass) or a
    genuine structural divergence (fresh-impulse / score / slow-burn /
    late-entry / daily-mover disagree despite identical structural inputs).

Only LIVE LONG rows are comparable: replay is long-only and confidence-
neutral, so live short rows (rule "shorts disabled") and live confidence blocks
are out of the structural-agreement denominator and reported separately.

Read-only over /data; never orders and writes nothing under /data.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter
from typing import Any, Optional

_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
BAR_MS = 5 * 60_000
_TOL_MS = BAR_MS  # align to the same closed 5m bar

# live rules that are entirely explained by replay's declared neutralization /
# population scope, NOT by structural-gate disagreement.
_CONF_PREFIXES = ("confidence<",)
_EXPECTED_RULES = {"shorts disabled"}


def _load_jsonl(pattern: str) -> list[dict[str, Any]]:
    out = []
    for f in sorted(glob.glob(pattern)):
        for line in open(f):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _norm_rule(r: str) -> str:
    r = (r or "").strip()
    for p in _CONF_PREFIXES:
        if r.startswith(p):
            return p + "*"  # collapse the varying 0.60/0.55 threshold
    return r


def _is_conf_rule(r: str) -> bool:
    return any((r or "").startswith(p) for p in _CONF_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--replay", default="")
    ap.add_argument("--live-pattern", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    replay_path = args.replay or os.path.join(args.data_dir, "decision_shadow_replay.jsonl")
    live_pattern = args.live_pattern or os.path.join(args.data_dir, "runner_gate_shadow.jsonl*")

    replay = [r for r in _load_jsonl(replay_path) if r.get("replay")]
    live = [r for r in _load_jsonl(live_pattern) if not r.get("replay")]

    rep_idx: dict[tuple[str, int], dict[str, Any]] = {}
    for r in replay:
        rep_idx[(r.get("coin"), int(r["ts"]) // BAR_MS)] = r

    total_live_long = 0
    live_shorts = 0
    matched = 0
    admit_agree = 0
    structural_disagree: list[dict[str, Any]] = []
    expected_conf_only = 0
    rule_pairs: Counter = Counter()
    comp_drift = []

    for lr in live:
        side = lr.get("side")
        rule = _norm_rule(lr.get("rule", ""))
        if side == "short" or rule in _EXPECTED_RULES:
            live_shorts += 1
            continue
        total_live_long += 1
        key = (lr.get("coin"), int(lr["ts"]) // BAR_MS)
        rr = rep_idx.get(key)
        if rr is None:
            # replay samples every step-bars and only Binance-mapped coins;
            # an unmatched live row is out of the replay grid, not a conflict.
            continue
        matched += 1
        l_admit = bool(lr.get("admitted"))
        r_admit = bool(rr.get("admitted"))
        lc, rc = lr.get("composite_score"), rr.get("composite_score")
        if lc is not None and rc is not None:
            comp_drift.append(abs(float(lc) - float(rc)))
        if l_admit == r_admit:
            admit_agree += 1
            if not l_admit:
                rule_pairs[(rule, _norm_rule(rr.get("rule", "")))] += 1
            continue
        # admitted differs: attribute
        if (not l_admit) and r_admit and _is_conf_rule(rule):
            # live blocked on AI confidence; replay neutralized it -> expected
            expected_conf_only += 1
            continue
        structural_disagree.append({
            "coin": key[0], "ts": lr.get("ts"),
            "live_admitted": l_admit, "replay_admitted": r_admit,
            "live_rule": rule, "replay_rule": _norm_rule(rr.get("rule", "")),
            "live_comp": lc, "replay_comp": rc,
        })
        rule_pairs[(rule, _norm_rule(rr.get("rule", "")))] += 1

    n_drift = len(comp_drift)
    report = {
        "meta": {
            "replay_rows": len(replay), "live_rows": len(live),
            "live_short_or_scanlong_blocked": live_shorts,
            "live_long_structural": total_live_long,
            "matched_on_coin_bar": matched,
            "note": "match set = live LONG verdicts on (coin,5m bar) present in "
                    "the replay grid. Replay is conf-neutralized/long-only.",
        },
        "agreement": {
            "admit_agree": admit_agree,
            "expected_conf_neutralization_differ": expected_conf_only,
            "structural_disagree": len(structural_disagree),
            "structural_agree_pct": round(
                100 * admit_agree / max(1, (matched - expected_conf_only)), 2),
            "composite_abs_drift_mean": round(sum(comp_drift) / n_drift, 3) if n_drift else None,
            "composite_abs_drift_max": round(max(comp_drift), 3) if n_drift else None,
        },
        "rule_pair_counts": {f"{k[0]}  ==>  {k[1]}": v for k, v in rule_pairs.most_common(20)},
        "structural_disagreements_sample": structural_disagree[:30],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
