#!/usr/bin/env python3
"""C8 (2026-11-03): per-gate funnel attribution for the 8→2→0 stall.

Answers two questions with the corrected (P0-3 direction-aware) outcomes:

  1. WHERE candidates are lost: normalize the live runner-gate `rule` strings
     (they embed varying thresholds) into gate families and count the funnel.
  2. WHETHER each gate's blocks were GOOD: forward net_bp19 (1h) of the
     candidates each family BLOCKED, vs ADMITTED. A gate that blocks strongly
     NEGATIVE-forward candidates is protective; blocking positive ones is
     over-tightening / regime mismatch.

Forward returns reuse the P0-3-corrected direction (short rows re-signed),
matching scripts/p2_1_weight_counterfactual.py. Outcomes only exist for rows
that reached research+gate, so the scan→research tier (ta_skip / cooldown /
throttle in events.jsonl) is reported as a count note, not joined here.

Read-only; writes nothing under /data.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics as st
from collections import Counter, defaultdict
from typing import Any, Optional

_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")


def _norm_rule(rule: str, admitted: bool) -> str:
    r = (rule or "").strip()
    if admitted or not r:
        return "ADMITTED"
    # live rule strings look like: "runner_gate_blocked (<reason>)"
    inner = r
    if inner.startswith("runner_gate_blocked"):
        inner = inner.split("(", 1)[1].rstrip(")") if "(" in inner else inner
    if "shorts disabled" in inner:
        return "shorts_disabled(scan_long_only)"
    if inner.startswith("confidence") or " < 0.60" in inner or "< 0.6" in inner:
        return "ai_confidence_floor"
    if "late trend-only chase" in inner:
        return "late_trend_only_chase"
    if inner.startswith("needs fresh"):
        return "fresh_impulse_gate"
    if "over-extended short" in inner or "ATR below EMA21" in inner:
        return "overextended_short"
    if "oversold" in inner and "RSI" in inner:
        return "rsi_oversold_short"
    if "pullback-long SHADOW" in inner:
        return "pullback_long_shadow_record"
    if "weak-trend chase" in inner:
        return "weak_trend_adx_shadow"
    if "reversal" in inner.lower():
        return "xs_reversal_zone"
    return "other:" + inner[:40]


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
        return -v - 2 * 0.0019  # undo long-sign + double cost (see p2_1 script)
    return v


def _block(xs: list[float], days: list[int], B: int = 4000, seed: int = 11):
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
    return {"n": len(xs), "days": k,
            "mean_bp": round(1e4 * st.mean(xs), 2),
            "ci95_bp": [round(1e4 * means[int(.025 * B)], 2),
                        round(1e4 * means[int(.975 * B)], 2)],
            "win_pct": round(100 * sum(1 for x in xs if x > 0) / len(xs), 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--horizon", default="1h")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    hz = args.horizon

    live = [r for r in _load(os.path.join(args.data_dir, "runner_gate_shadow.jsonl*"))
            if not r.get("replay")]
    outcomes = {}
    for line in open(os.path.join(args.data_dir, "decision_shadow_outcomes.jsonl")):
        try:
            o = json.loads(line)
        except Exception:
            continue
        outcomes[(o.get("ts"), o.get("coin"), o.get("side"))] = o

    funnel = Counter()
    vals: dict[str, list[float]] = defaultdict(list)
    days: dict[str, list[int]] = defaultdict(list)
    joined = 0
    for r in live:
        fam = _norm_rule(r.get("rule", ""), bool(r.get("admitted")))
        funnel[fam] += 1
        o = outcomes.get((r.get("ts"), r.get("coin"), r.get("side")))
        if not o or not o.get("covered"):
            continue
        v = _signed_net(o, hz)
        if v is None:
            continue
        vals[fam].append(v)
        days[fam].append(int(r["ts"]) // 86_400_000)
        joined += 1

    total = sum(funnel.values())
    table = []
    for fam, cnt in funnel.most_common():
        stat = _block(vals[fam], days[fam])
        table.append({"gate": fam, "rows": cnt, "pct_of_rows": round(100 * cnt / total, 1),
                      "forward_net_bp19": stat})

    report = {
        "meta": {"horizon": hz, "live_rows": total,
                 "outcomes_joined": joined,
                 "short_sign": "P0-3-corrected",
                 "interpretation": "negative forward mean for a BLOCKED gate = "
                                   "protective (it removed losers); positive = "
                                   "gate may be removing winners."},
        "funnel": table,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
