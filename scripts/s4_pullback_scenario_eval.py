#!/usr/bin/env python3
"""S4-3 / S4-4 offline read-only evaluation.

S4-3 — pullback_long real-candidate assessment:
  Reads the reconciled pullback shadow JSONL (each row already carries a
  paper ``outcome`` / ``pnl_pct`` from reconcile_pullback_shadow.py) and
  decides whether the pullback entry zone has enough evidence to become a
  real (non-shadow) candidate. High win-rate zones require S2 per-hit /
  pullback evidence — parameters are never fitted here.

S4-4 — full-scenario summary layer:
  Groups reconciled trades across regime (when present) and hold-duration
  buckets at three cost tiers (gross / net@3 maker / net@9.26 taker). This
  is a summary/stratification layer over already-simulated DSL exits, not a
  new fill simulator — the PIT DSL replay in reconcile_pullback_shadow /
  bt_ra_exch remains the single backtest engine.

Pure offline analysis. Output is one JSON file under /tmp. No network, no
order placement, no production mutation.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

COSTS_BP = (3.0, 9.26)
MIN_SAMPLE = 30
# Hold-duration buckets in hours (closed trades).
DURATION_BUCKETS_HOURS = ((0, 4), (4, 24), (24, 72), (72, 10 ** 9))
DURATION_LABELS = ("lt_4h", "4h_to_24h", "24h_to_72h", "gte_72h")
FIXED_HORIZONS_HOURS = (4, 12, 24, 72)
SCENARIO_REGIMES = ("trend", "chop", "reversal", "unknown")
WFA_TRAIN_MONTHS = 12
WFA_TEST_MONTHS = 4


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _hold_hours(row: dict[str, Any]) -> Optional[float]:
    for k_in, k_out in (("entry_ts", "exit_ts"), ("signal_ts", "exit_ts")):
        ti, to = row.get(k_in), row.get(k_out)
        if finite(ti) and finite(to) and to >= ti:
            return (float(to) - float(ti)) / 3_600_000.0
    return None


def _duration_bucket(hold_h: Optional[float]) -> str:
    if hold_h is None:
        return "unknown"
    for (lo, hi), label in zip(DURATION_BUCKETS_HOURS, DURATION_LABELS):
        if lo <= hold_h < hi:
            return label
    return "unknown"


def _wilson_interval(successes: int, n: int,
                     z: float = 1.959963984540054) -> list[Optional[float]]:
    if n <= 0:
        return [None, None]
    p = successes / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 4),
            round(min(1.0, centre + half), 4)]


def _metric_summary(bp: list[float]) -> dict[str, Any]:
    if not bp:
        return {"n": 0, "median_bp": None, "mean_bp": None,
                "win_rate": None, "win_rate_wilson95": [None, None]}
    wins = sum(1 for v in bp if v > 0)
    return {
        "n": len(bp),
        "median_bp": round(statistics.median(bp), 4),
        "mean_bp": round(statistics.fmean(bp), 4),
        "win_rate": round(wins / len(bp), 4),
        "win_rate_wilson95": _wilson_interval(wins, len(bp)),
    }


def _summarize_pct(pnl_pct_rows: list[float]) -> dict[str, Any]:
    """Summarize gross percent rows at gross / maker 3bp / taker 9.26bp."""
    bp = [v * 100.0 for v in pnl_pct_rows]
    gross = _metric_summary(bp)
    net3 = _metric_summary([v - 3.0 for v in bp])
    net926 = _metric_summary([v - 9.26 for v in bp])
    return {
        "n": len(bp),
        "win_rate": gross["win_rate"],
        "win_rate_wilson95": gross["win_rate_wilson95"],
        "gross_median_bp": gross["median_bp"],
        "gross_mean_bp": gross["mean_bp"],
        "net_3_median_bp": net3["median_bp"],
        "net_3_mean_bp": net3["mean_bp"],
        "net_9_26_median_bp": net926["median_bp"],
        "net_9_26_mean_bp": net926["mean_bp"],
        "costs": {"gross": gross, "net_3": net3, "net_9_26": net926},
    }


def _scenario_regime(row: dict[str, Any]) -> str:
    raw = str(row.get("regime") or row.get("macro_regime")
              or row.get("entry_regime") or "unknown").lower()
    if raw in ("up", "down", "trend", "trending"):
        return "trend"
    if raw in ("chop", "choppy", "range", "ranging", "neutral"):
        return "chop"
    if raw in ("reversal", "reversing"):
        return "reversal"
    return "unknown"


def _fixed_horizon(hold_h: Optional[float]) -> str:
    if hold_h is None:
        return "unknown"
    for horizon in FIXED_HORIZONS_HOURS:
        if hold_h <= horizon:
            return f"{horizon}h"
    return "gt_72h"


def _exit_month(row: dict[str, Any]) -> Optional[str]:
    ts = row.get("exit_ts")
    if not finite(ts):
        return None
    return datetime.fromtimestamp(float(ts) / 1000, timezone.utc).date().isoformat()[:7]


def _gross_pct(row: dict[str, Any]) -> tuple[float, str]:
    """Prefer explicit gross; otherwise undo reconcile's one configured fee.

    Legacy rows without fee metadata retain the historic gross assumption.
    """
    if finite(row.get("gross_pnl_pct")):
        return float(row["gross_pnl_pct"]), "explicit_gross"
    pnl = float(row["pnl_pct"])
    if finite(row.get("fee_pct")):
        return pnl + float(row["fee_pct"]), "derived_from_fee_net"
    return pnl, "assumed_gross_legacy"


def _scenario_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(_scenario_regime(row), _fixed_horizon(_hold_hours(row)))].append(
            float(row["_gross_pct"]))
    matrix = {
        regime: {
            f"{horizon}h": _summarize_pct(
                grouped.get((regime, f"{horizon}h"), []))
            for horizon in FIXED_HORIZONS_HOURS
        }
        for regime in SCENARIO_REGIMES
    }
    populated = [
        {"regime": regime, "horizon": horizon, **stats}
        for regime, horizons in matrix.items()
        for horizon, stats in horizons.items() if stats["n"] > 0
    ]
    worst = min(populated, key=lambda cell: cell["net_9_26_mean_bp"],
                default=None)
    return {
        "matrix": matrix,
        "horizons_hours": list(FIXED_HORIZONS_HOURS),
        "regimes": list(SCENARIO_REGIMES),
        "assignment": "first fixed horizon >= observed hold duration; >72h and unknown are excluded",
        "complete_n_zero_cells": True,
        "worst_case_metric": "net_9_26_mean_bp",
        "worst_case": worst,
        "excluded_rows": {
            "gt_72h": sum(1 for r in rows if _fixed_horizon(_hold_hours(r)) == "gt_72h"),
            "unknown_duration": sum(1 for r in rows if _hold_hours(r) is None),
        },
    }


def _out_of_sample_acceptance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dated = [(month, row) for row in rows if (month := _exit_month(row)) is not None]
    months = sorted({month for month, _ in dated})
    folds = []
    for start in range(0, len(months) - WFA_TRAIN_MONTHS - WFA_TEST_MONTHS + 1,
                       WFA_TEST_MONTHS):
        train_months = months[start:start + WFA_TRAIN_MONTHS]
        test_months = months[start + WFA_TRAIN_MONTHS:
                             start + WFA_TRAIN_MONTHS + WFA_TEST_MONTHS]
        train = [r for month, r in dated if month in train_months]
        test = [r for month, r in dated if month in test_months]
        train_stats = _summarize_pct([float(r["_gross_pct"]) for r in train])
        test_stats = _summarize_pct([float(r["_gross_pct"]) for r in test])
        test_matrix = _scenario_matrix(test)
        worst = test_matrix["worst_case"]
        passes = bool(
            test_stats["n"] >= MIN_SAMPLE
            and (test_stats["win_rate"] or 0) > 0.5
            and (test_stats["net_9_26_median_bp"] or 0) > 0
            and worst is not None and worst["net_9_26_mean_bp"] > 0
        )
        folds.append({
            "train_months": train_months,
            "test_months": test_months,
            "train": train_stats,
            "test": test_stats,
            "test_worst_case": worst,
            "pass": passes,
        })
    if not folds:
        decision = "inconclusive"
        reason = f"requires at least {WFA_TRAIN_MONTHS + WFA_TEST_MONTHS} distinct exit months"
    elif all(f["pass"] for f in folds):
        decision = "pass"
        reason = "all 12/4 out-of-sample folds satisfy the pre-registered gates"
    else:
        decision = "fail"
        reason = "at least one out-of-sample fold failed n/win-rate/taker-edge/worst-case"
    return {
        "decision": decision,
        "reason": reason,
        "train_months": WFA_TRAIN_MONTHS,
        "test_months": WFA_TEST_MONTHS,
        "step_months": WFA_TEST_MONTHS,
        "folds": folds,
        "gates": {"min_test_n": MIN_SAMPLE, "win_rate_gt": 0.5,
                  "net_9_26_median_bp_gt": 0.0,
                  "worst_cell_net_9_26_mean_bp_gt": 0.0},
    }


def evaluate(pullback_path: Path) -> dict[str, Any]:
    rows = load_jsonl(pullback_path)
    reconciled = []
    cost_basis_counts: Counter[str] = Counter()
    for row in rows:
        if row.get("outcome") not in ("win", "loss") or not finite(row.get("pnl_pct")):
            continue
        gross, basis = _gross_pct(row)
        normalized = dict(row)
        normalized["_gross_pct"] = gross
        reconciled.append(normalized)
        cost_basis_counts[basis] += 1
    pending = [r for r in rows if r.get("outcome") not in ("win", "loss")]

    # ── S4-3: aggregate pullback candidate evidence ──────────────────────
    overall = _summarize_pct([float(r["_gross_pct"]) for r in reconciled])
    by_regime: dict[str, list[float]] = defaultdict(list)
    by_duration: dict[str, list[float]] = defaultdict(list)
    by_combo: dict[str, list[float]] = defaultdict(list)
    for r in reconciled:
        regime = str(r.get("regime") or r.get("macro_regime") or "unknown").lower()
        pnl = float(r["_gross_pct"])
        hold_h = _hold_hours(r)
        bucket = _duration_bucket(hold_h)
        by_regime[regime].append(pnl)
        by_duration[bucket].append(pnl)
        by_combo[f"{regime}|{bucket}"].append(pnl)

    regime_summary = {k: _summarize_pct(v) for k, v in sorted(by_regime.items())}
    duration_summary = {k: _summarize_pct(v) for k, v in sorted(by_duration.items())}
    combo_summary = {k: _summarize_pct(v) for k, v in sorted(by_combo.items())}

    # Pre-registered promotion rule for the pullback zone (no param fitting):
    # n>=30, win-rate > 0.5, and net@9.26 median > 0. Otherwise it stays shadow.
    n = overall.get("n", 0)
    wr = overall.get("win_rate")
    net926 = overall.get("net_9_26_median_bp")
    if n < MIN_SAMPLE:
        decision = "inconclusive"
        reason = f"n={n} < {MIN_SAMPLE}"
    elif wr is not None and wr > 0.5 and net926 is not None and net926 > 0:
        decision = "candidate_promote"
        reason = "n>=30, win_rate>0.5, net@9.26 median>0"
    else:
        decision = "keep_shadow"
        reason = (f"evidence insufficient: win_rate={wr}, "
                  f"net@9.26_median_bp={net926}")

    outcome_counts = Counter(str(r.get("outcome")) for r in reconciled)

    return {
        "schema_version": "s4_pullback_scenario_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_read_only",
        "input": str(pullback_path),
        "s4_3_pullback_candidate": {
            "decision": decision,
            "reason": reason,
            "preregistration": {
                "min_sample": MIN_SAMPLE,
                "win_rate_gt": 0.5,
                "net_9_26_median_bp_gt": 0.0,
                "no_parameter_fitting": True,
            },
            "overall": overall,
            "outcome_counts": dict(outcome_counts),
            "input_cost_basis_counts": dict(cost_basis_counts),
            "by_btc_regime": regime_summary,
        },
        "s4_4_scenario_stratification": {
            "by_hold_duration": duration_summary,
            "by_regime_and_duration": combo_summary,
            "fixed_scenarios": _scenario_matrix(reconciled),
            "out_of_sample_acceptance": _out_of_sample_acceptance(reconciled),
            "cost_tiers_bp": {"maker": 3.0, "taker": 9.26},
            "duration_buckets_hours": {
                label: list(bounds)
                for label, bounds in zip(DURATION_LABELS, DURATION_BUCKETS_HOURS)
            },
        },
        "pending_unreconciled_rows": len(pending),
        "total_rows": len(rows),
        "limitations": [
            "regime only available when the shadow record captured it; otherwise unknown",
            "pullback shadow is long-only until S3 shorts accumulate",
            "duration buckets require entry/exit timestamps; missing → unknown bucket",
            "this is a summary layer; the PIT DSL replay engine is unchanged",
            "fixed horizons are observed-duration strata, not newly simulated exits",
            "12/4 acceptance remains inconclusive until at least 16 distinct exit months exist",
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pullback-file",
                   default=os.environ.get("HERMES_PULLBACK_SHADOW_FILE",
                                          "/data/pullback_shadow.jsonl"),
                   type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    out = args.output.resolve()
    if out.parent != Path("/tmp"):
        p.error("--output must be a file directly under /tmp")
    result = evaluate(args.pullback_file)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(tmp, out)
    print(json.dumps({
        "output": str(out),
        "decision": result["s4_3_pullback_candidate"]["decision"],
        "n": result["s4_3_pullback_candidate"]["overall"].get("n", 0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
