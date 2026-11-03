#!/usr/bin/env python3
"""P2-1a (2026-11-03): trigger-weight table vs fire-rate counterfactual audit.

The v2 review (Hermes_统一版v2) flagged structural decoupling of the composite
weight table from the triggers that actually fire:

  * slowBurn is the most frequent fired label but is structurally NOT a base
    trigger (it is an executor-layer observation tag; see
    executor._runner_gate_fired_names), so it can never enter the composite
    numerator under ANY weight table;
  * production (deep-merged deploy/prod_override.json) still pins the OLD
    2026-06 weight set (trend_strength 0.55, uptrend_momentum 0.0, sum 1.85),
    while CANONICAL_DEFAULTS already carries the 2026-09-28 one-year event-study
    reweight (trend_strength 0.30, uptrend_momentum 0.20, sum 1.80).

WHAT THIS SCRIPT CAN AND CANNOT DECIDE
--------------------------------------
composite_score = 10 * sum(score_i * w_i for FIRED) / sum(ALL w). The persisted
rows store only each trigger's fired BOOLEAN and the composite computed under
the OLD table — never the per-trigger continuous score_i (decision snapshots
store strength=null). Therefore the NEW-table ABSOLUTE composite of a historical
candidate cannot be reconstructed offline, and a "which table selects better"
verdict would require either re-running selection or adding hit-score capture.
This script stays within what the data strictly supports:

  1. fire-rate x weight mismatch for OLD(prod) vs NEW(canonical) tables;
  2. weight-sum / single-trigger-ceiling arithmetic and the gate re-calibration
     a table switch would force;
  3. forward edge of each fired-label GROUP from the recorded (old-table)
     composite population, with the P0-3 short sign corrected IN MEMORY (the
     production outcomes predate the P0-3 fix). This ranks whether the active
     signals carry edge; it is NOT a new-table selection backtest.

Read-only: reads /data runner_gate_shadow + decision_shadow_outcomes, writes
nothing in /data. Never places an order.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
from collections import defaultdict
from typing import Any, Optional

_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")

# flag key on the runner-gate row -> canonical trigger name
_FLAG_TO_NAME = [
    ("vol", "volumeSpike"), ("brk", "breakout"), ("burst", "momentumBurst"),
    ("dMover", "dailyMover"), ("up", "uptrendMomentum"),
    ("down", "downtrendMomentum"),
]
# observation-only tag (NOT a composite base trigger)
SLOWBURN = "slowBurn"

# OLD table = production prod_override.json (camelCase runtime values).
# NEW table = canonical config.py TRIGGER_CONFIG 2026-09-28 reweight.
OLD_WEIGHTS = {  # prod live (deep-merged override), sum 1.85
    "trendStrength": 0.55, "pctMoveSpike": 0.40, "breakout": 0.30,
    "volumeSpike": 0.25, "momentumBurst": 0.20, "volumeBuildup1h": 0.15,
    "higherLows1h": 0.0, "trendFlip1h": 0.0, "rangeCompression": 0.0,
    "uptrendMomentum": 0.0, "downtrendMomentum": 0.0, "dailyMover": 0.0,
}
NEW_WEIGHTS = {  # canonical 2026-09-28 event-study reweight, sum 1.80
    "trendStrength": 0.30, "pctMoveSpike": 0.40, "breakout": 0.30,
    "volumeSpike": 0.25, "momentumBurst": 0.20, "volumeBuildup1h": 0.15,
    "higherLows1h": 0.0, "trendFlip1h": 0.0, "rangeCompression": 0.0,
    "uptrendMomentum": 0.20, "downtrendMomentum": 0.0, "dailyMover": 0.0,
}
# production admission gate observed on the rows (min_composite) and the code's
# recalibrated selectivity gate for a 1.80 denom (config.py minCompositeScore).
PROD_GATE = 45.0
CANON_GATE = 54.0


def _load_source_rows(pattern: str) -> list[dict[str, Any]]:
    rows = []
    for f in sorted(glob.glob(pattern)):
        for line in open(f):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue  # one torn row costs only itself (4b26f75 convention)
    return rows


def _load_snapshot_fire_rates(data_dir: str) -> tuple[dict[str, int], int]:
    """True per-trigger fire rates from decision snapshots.

    The runner-gate shadow rows only persist SIX booleans (vol/brk/burst/
    dMover/up/down) + slow_burn_count; trendStrength / pctMoveSpike /
    volumeBuildup1h / higherLows1h / trendFlip1h / rangeCompression are NOT
    recorded there, so their gate-row fire rate reads 0 by omission — the same
    missing-as-zero trap the v2 review warned about. Decision snapshots persist
    the full 12-trigger fired array and are the authoritative fire-rate source.
    """
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for d in sorted(glob.glob(os.path.join(data_dir, "decision-snapshots", "date=*"))):
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            for line in open(f):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                total += 1
                for t in r.get("triggers", []):
                    if t.get("fired"):
                        counts[t.get("name")] += 1
    return counts, total


def _load_outcomes(path: str) -> dict[tuple, dict[str, Any]]:
    out: dict[tuple, dict[str, Any]] = {}
    if not os.path.isfile(path):
        return out
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        out[(r.get("ts"), r.get("coin"), r.get("side"))] = r
    return out


def _signed(rec: dict[str, Any], field: str, hz: str) -> Optional[float]:
    """Forward return with the P0-3 short-direction fix applied in memory.

    Production outcomes predate P0-3: gross_fwd is long-direction for every
    row. Re-sign here so short rows reflect PnL (price up => short loss). Cost
    was already charged once by enrich and stays as-is (sign-independent).
    """
    blk = rec.get(field)
    if not isinstance(blk, dict):
        return None
    v = blk.get(hz)
    if v is None:
        return None
    if str(rec.get("side")).lower() == "short":
        # gross-style blocks (gross_fwd) flip; net/excess also flip their raw
        # leg, but cost was subtracted, so flip then re-add the double-count:
        # signed_net = -(raw_gross - cost) - cost = -raw_gross + cost - cost.
        if field == "gross_fwd":
            return -v
        if field.startswith("net_"):
            return -v - 2 * (0.0019 if field == "net_bp19" else 0.000926)
        if field.startswith("excess_"):
            cost = 0.0019 if field == "excess_bp19" else 0.000926
            return -(v + cost) - cost
    return v


def _dayblock(xs: list[float], days: list[int], B: int = 4000, seed: int = 7):
    if len(xs) < 2:
        return {"n": len(xs), "days": len(set(days)), "mean_bp": None,
                "ci95_bp": [None, None]}
    import random
    byd: dict[int, list[float]] = defaultdict(list)
    for x, d in zip(xs, days):
        byd[d].append(x)
    blocks = [st.mean(v) for v in byd.values()]
    k = len(blocks)
    rnd = random.Random(seed)
    means = []
    for _ in range(B):
        means.append(st.mean(rnd.choice(blocks) for _ in range(k)))
    means.sort()
    return {"n": len(xs), "days": k, "mean_bp": round(1e4 * st.mean(xs), 2),
            "ci95_bp": [round(1e4 * means[int(.025 * B)], 2),
                        round(1e4 * means[int(.975 * B)], 2)]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=_DATA_DIR)
    ap.add_argument("--horizon", default="1h")
    ap.add_argument("--out", default="", help="optional JSON report path")
    args = ap.parse_args()
    hz = args.horizon

    rows = _load_source_rows(os.path.join(args.data_dir, "runner_gate_shadow.jsonl*"))
    outcomes = _load_outcomes(os.path.join(args.data_dir, "decision_shadow_outcomes.jsonl"))
    n = len(rows)
    snap_fires, snap_n = _load_snapshot_fire_rates(args.data_dir)

    # ── 1. fire rates ────────────────────────────────────────────────────────
    # Per-trigger rates come from decision snapshots (full 12-trigger array).
    # Gate-row booleans below cover only six flags and are used solely for the
    # slowBurn structural-tag count and the forward-edge grouping.
    fires: dict[str, int] = defaultdict(int)
    slow = 0
    for r in rows:
        for key, name in _FLAG_TO_NAME:
            if r.get(key):
                fires[name] += 1
        if int(r.get("slow_burn_count") or 0) >= 1:
            slow += 1
    fires[SLOWBURN] = slow

    def weight_table(tbl: dict[str, float]) -> list[dict[str, Any]]:
        rows_out = []
        for name in sorted(set(tbl) | {SLOWBURN}):
            if name == SLOWBURN:
                # structural gate-row tag: count + denominator are gate rows
                fr, denom, note = slow, n, "gate-row population"
            else:
                fr, denom, note = snap_fires.get(name, 0), snap_n, "snapshot population"
            w = tbl.get(name)
            rows_out.append({
                "trigger": name, "weight": w,
                "fire_rate_pct": round(100 * fr / denom, 1) if denom else None,
                "fires": fr, "population": note,
                "in_composite": name != SLOWBURN,
            })
        return rows_out

    # ── 2. arithmetic ────────────────────────────────────────────────────────
    old_sum, new_sum = sum(OLD_WEIGHTS.values()), sum(NEW_WEIGHTS.values())

    def ceiling(tbl: dict[str, float], w: float) -> float:
        # best possible score if the given weight fires alone at a full hit=10
        return round(10 * w / sum(tbl.values()), 2)

    # ── 3. forward edge per fired-label GROUP (old-table population) ─────────
    groups: dict[str, list[float]] = defaultdict(list)
    gdays: dict[str, list[int]] = defaultdict(list)
    used = 0
    for r in rows:
        key = (r.get("ts"), r.get("coin"), r.get("side"))
        o = outcomes.get(key)
        if not o or not o.get("covered"):
            continue
        v = _signed(o, "net_bp19", hz)
        if v is None:
            continue
        labels = list(o.get("fired_names") or [])
        if not labels:
            labels = ["none"]
        d = int(r.get("ts")) // 86_400_000
        for lb in set(labels):
            groups[lb].append(v)
            gdays[lb].append(d)
        used += 1
    edge = {lb: _dayblock(groups[lb], gdays[lb]) for lb in sorted(groups)}

    report = {
        "meta": {
            "horizon": hz, "source_rows": n,
            "snapshot_rows_for_fire_rate": snap_n,
            "outcomes_joined_covered": used,
            "short_sign": "P0-3-corrected-in-memory",
            "fire_rate_source": "decision-snapshots (full 12 triggers); "
                                "gate rows persist only 6 booleans + slowBurn",
            "note": "group edge is over the recorded OLD-table population; "
                    "NEW-table absolute composites cannot be rebuilt offline "
                    "(per-hit scores were never persisted).",
        },
        "arithmetic": {
            "old_weight_sum": old_sum, "new_weight_sum": new_sum,
            "prod_gate": PROD_GATE, "canonical_gate": CANON_GATE,
            "single_trigger_ceiling": {
                "old_pctMoveSpike_0.40": ceiling(OLD_WEIGHTS, 0.40),
                "new_pctMoveSpike_0.40": ceiling(NEW_WEIGHTS, 0.40),
                "new_uptrendMomentum_0.20": ceiling(NEW_WEIGHTS, 0.20),
            },
        },
        "old_table_prod": weight_table(OLD_WEIGHTS),
        "new_table_canonical": weight_table(NEW_WEIGHTS),
        "forward_edge_net_bp19_by_fired_label": edge,
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
