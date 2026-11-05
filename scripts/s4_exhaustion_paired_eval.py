#!/usr/bin/env python3
"""S4-2 offline read-only: exhaustion-signal virtual exits vs real DSL exits.

Reads the S1-3 exit-counterfactual JSONL (CVD divergence / phase-2 floor
breach virtual exits) and the shadow book's real fills, then computes a paired
net-bp delta for every position that has BOTH a virtual exit and a real close.

Pure offline analysis — never writes to the shadow book, never places orders,
never imports a production module that mutates state. Output is one JSON file
under /tmp.

Pre-registered decision rule (from the S4 plan):
  * a signal arm only replaces the DSL exit when the paired net-delta day-block
    bootstrap 95% CI lower bound > 0 AND the leave-one-month-out worst month is
    non-negative;
  * arms with n < 30 paired positions are marked inconclusive regardless of
    point estimate;
  * costs are applied identically to both legs (the delta is cost-neutral at
    the same fee tier, but gross and both net columns are reported).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BAR_5M_MS = 300_000
COSTS_BP = (3.0, 9.26)
BOOTSTRAP_N = 2000
SEED = 20261105
MIN_PAIRED = 30
SIGNALS = (
    "cvd_divergence",
    "phase2_trailing_floor",
    "adx_atr_decay",
    "volume_stagnation",
)
SIGNAL_ALIASES = {"phase2_floor_breach": "phase2_trailing_floor"}


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def side_sign(side: str) -> int:
    return -1 if str(side).lower() == "short" else 1


def directional_bp(entry: float, exit_px: float, side: str) -> float:
    return side_sign(side) * (float(exit_px) - float(entry)) / float(entry) * 10_000.0


def wilson_interval(successes: int, n: int,
                    z: float = 1.959963984540054) -> tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = successes / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def day_block_bootstrap(values: list[float], days: list[str],
                        seed: int) -> dict[str, Any]:
    """Multi-day block bootstrap: resample whole coin-days, never individual rows."""
    if not values:
        return {"n": 0, "blocks": 0, "mean_bp": None, "ci95_bp": [None, None]}
    by_day: dict[str, list[float]] = defaultdict(list)
    for v, d in zip(values, days):
        by_day[d].append(v)
    block_means = [statistics.fmean(vs) for vs in by_day.values()]
    point = statistics.fmean(block_means)
    n_blocks = len(block_means)
    if n_blocks < 2:
        return {"n": len(values), "blocks": n_blocks, "mean_bp": round(point, 4),
                "ci95_bp": [None, None]}
    rng = random.Random(seed)
    boots = []
    for _ in range(BOOTSTRAP_N):
        sample = [block_means[rng.randrange(n_blocks)] for _ in range(n_blocks)]
        boots.append(statistics.fmean(sample))
    boots.sort()
    lo = boots[int(0.025 * BOOTSTRAP_N)]
    hi = boots[int(0.975 * BOOTSTRAP_N)]
    return {"n": len(values), "blocks": n_blocks,
            "mean_bp": round(point, 4),
            "ci95_bp": [round(lo, 4), round(hi, 4)]}


def load_counterfactuals(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("observer_only") and finite(row.get("mid")):
                rows.append(row)
    return rows


def load_shadow_book(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"accounts": {}}
    with path.open() as f:
        return json.load(f)


def _position_id_from_fill(fill: dict[str, Any]) -> Optional[str]:
    return fill.get("position_id") or fill.get("pos_id")


def build_real_close_index(
    book: dict[str, Any],
    issues: Optional[dict[str, Any]] = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Map (account, position_id) -> real close; quarantine duplicate closes."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_keys: set[tuple[str, str]] = set()
    for acct_name, acct in (book.get("accounts") or {}).items():
        entries: dict[str, dict[str, Any]] = {}
        for fill in acct.get("fills", []):
            pid = _position_id_from_fill(fill)
            if pid is None:
                continue
            pid = str(pid)
            key = (str(acct_name), pid)
            if fill.get("type") == "open":
                entries[pid] = fill
            elif fill.get("type") == "close":
                if key in index or key in duplicate_keys:
                    index.pop(key, None)
                    duplicate_keys.add(key)
                    continue
                entry = entries.get(pid, {})
                index[key] = {
                    "account": str(acct_name),
                    "position_id": pid,
                    "coin": fill.get("coin") or entry.get("coin"),
                    "side": (fill.get("side") or entry.get("side") or "").lower(),
                    "entry_px": entry.get("price") or entry.get("mid_px"),
                    "real_exit_px": fill.get("price"),
                    "real_exit_ts": fill.get("ts"),
                    "real_reason": fill.get("reason", ""),
                    "entry_fill": entry,
                    "close_fill": fill,
                }
    if issues is not None:
        issues["duplicate_real_close_keys"] = sorted(duplicate_keys)
    return index


def evaluate(data_dir: Path,
             book_path: Optional[Path] = None,
             cf_path: Optional[Path] = None) -> dict[str, Any]:
    book_path = book_path or (data_dir / ".shadow-book.json")
    cf_path = cf_path or (data_dir / ".shadow-exit-counterfactual.jsonl")
    book = load_shadow_book(book_path)
    cfs = load_counterfactuals(cf_path)
    pairing_issues: dict[str, Any] = {}
    real_index = build_real_close_index(book, pairing_issues)
    duplicate_real_keys = set(pairing_issues["duplicate_real_close_keys"])

    # Paired rows: one per (account, position, signal). Legacy rows without an
    # account are admitted only when the position id is unique across accounts.
    paired_by_signal: dict[str, list[dict[str, Any]]] = {s: [] for s in SIGNALS}
    unmatched_virtual = 0
    ambiguous_virtual = 0
    duplicate_virtual = 0
    mismatched_virtual = 0
    seen_virtual: set[tuple[str, str, str]] = set()
    for cf in cfs:
        raw_signal = str(cf.get("signal", ""))
        signal = SIGNAL_ALIASES.get(raw_signal, raw_signal)
        if signal not in paired_by_signal:
            continue
        pid = str(cf.get("position_id") or cf.get("pos_id") or "")
        account = str(cf.get("account") or "")
        if not pid:
            unmatched_virtual += 1
            continue
        if account:
            key = (account, pid)
            if key in duplicate_real_keys:
                ambiguous_virtual += 1
                continue
            candidates = [real_index[key]] if key in real_index else []
        else:
            candidates = [r for (acct, pos), r in real_index.items()
                          if pos == pid and (acct, pos) not in duplicate_real_keys]
        if not candidates:
            unmatched_virtual += 1
            continue
        if len(candidates) != 1:
            ambiguous_virtual += 1
            continue
        real = candidates[0]
        if not finite(real.get("entry_px")) or not finite(real.get("real_exit_px")):
            unmatched_virtual += 1
            continue
        if ((cf.get("coin") and str(cf["coin"]).upper() != str(real["coin"]).upper())
                or (cf.get("side") and str(cf["side"]).lower() != real["side"])):
            mismatched_virtual += 1
            continue
        pair_key = (real["account"], pid, signal)
        if pair_key in seen_virtual:
            duplicate_virtual += 1
            continue
        seen_virtual.add(pair_key)
        entry = float(real["entry_px"])
        side = real["side"]
        virtual_px = float(cf["mid"])
        virtual_gross = directional_bp(entry, virtual_px, side)
        real_gross = directional_bp(entry, float(real["real_exit_px"]), side)
        delta = virtual_gross - real_gross
        ts = cf.get("virtual_exit_ts") or cf.get("ts")
        day = (datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()
               if finite(ts) else "unknown")
        paired_by_signal[signal].append({
            "position_id": pid,
            "account": real["account"],
            "coin": real["coin"],
            "side": side,
            "day": day,
            "virtual_gross_bp": round(virtual_gross, 4),
            "real_gross_bp": round(real_gross, 4),
            "delta_bp": round(delta, 4),
            "virtual_net_3_bp": round(virtual_gross - 3.0, 4),
            "real_net_3_bp": round(real_gross - 3.0, 4),
            "virtual_net_9_26_bp": round(virtual_gross - 9.26, 4),
            "real_net_9_26_bp": round(real_gross - 9.26, 4),
            "real_reason": real["real_reason"],
            "cf_reason": cf.get("reason", ""),
        })

    arms: dict[str, Any] = {}
    for signal, rows in paired_by_signal.items():
        if not rows:
            arms[signal] = {"n": 0, "status": "no_data",
                            "decision": "inconclusive"}
            continue
        deltas = [r["delta_bp"] for r in rows]
        days = [r["day"] for r in rows]
        virt_gross = [r["virtual_gross_bp"] for r in rows]
        real_gross = [r["real_gross_bp"] for r in rows]
        boot = day_block_bootstrap(deltas, days, SEED)
        # Leave-one-month-out worst month.
        by_month: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            month = r["day"][:7] if len(r["day"]) >= 7 else "unknown"
            by_month[month].append(r["delta_bp"])
        lomo = {m: round(statistics.fmean(v), 4) for m, v in sorted(by_month.items())}
        worst_month = min(lomo.values()) if lomo else None
        positive = sum(1 for d in deltas if d > 0)
        wp_lo, wp_hi = wilson_interval(positive, len(deltas))
        ci_lower = boot["ci95_bp"][0]
        meets_ci = ci_lower is not None and ci_lower > 0
        meets_lomo = worst_month is not None and worst_month >= 0
        meets_n = len(rows) >= MIN_PAIRED
        if not meets_n:
            decision = "inconclusive"
            status = "insufficient_n"
        elif meets_ci and meets_lomo:
            decision = "candidate_replace"
            status = "pass"
        else:
            decision = "keep_dsl"
            status = "fail"
        arms[signal] = {
            "n": len(rows),
            "status": status,
            "decision": decision,
            "delta_median_bp": round(statistics.median(deltas), 4),
            "delta_mean_bp": round(statistics.fmean(deltas), 4),
            "virtual_gross_median_bp": round(statistics.median(virt_gross), 4),
            "real_gross_median_bp": round(statistics.median(real_gross), 4),
            "cost_tiers": {
                str(cost): {
                    "virtual_net_median_bp": round(
                        statistics.median(virt_gross) - cost, 4),
                    "real_net_median_bp": round(
                        statistics.median(real_gross) - cost, 4),
                    "paired_net_delta_median_bp": round(
                        statistics.median(deltas), 4),
                }
                for cost in COSTS_BP
            },
            "win_rate_delta_positive": round(positive / len(rows), 4),
            "win_rate_wilson95": [round(wp_lo, 4) if wp_lo is not None else None,
                                  round(wp_hi, 4) if wp_hi is not None else None],
            "block_bootstrap": boot,
            "leave_one_month_out": lomo,
            "worst_month_mean_delta_bp": worst_month,
            "preregistration": {
                "min_paired": MIN_PAIRED,
                "ci95_lower_must_be_gt_0": meets_ci,
                "worst_month_non_negative": meets_lomo,
                "n_gate_passed": meets_n,
            },
            "rows": rows,
        }

    return {
        "schema_version": "s4_exhaustion_paired_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_read_only",
        "inputs": {
            "shadow_book": str(book_path),
            "counterfactual_jsonl": str(cf_path),
        },
        "counterfactual_rows_total": len(cfs),
        "unmatched_virtual_exits": unmatched_virtual,
        "ambiguous_virtual_exits": ambiguous_virtual,
        "duplicate_virtual_exits": duplicate_virtual,
        "mismatched_virtual_exits": mismatched_virtual,
        "duplicate_real_close_keys": [
            {"account": account, "position_id": pid}
            for account, pid in pairing_issues["duplicate_real_close_keys"]
        ],
        "real_closed_positions": len(real_index),
        "pairing_key": ["account", "position_id", "signal"],
        "legacy_signal_aliases": SIGNAL_ALIASES,
        "signals_evaluated": list(SIGNALS),
        "arms": arms,
        "cost_note": "delta is cost-neutral at equal fee tier; gross, net@3 and net@9.26 are reported",
        "limitations": [
            "current production shadow book is LONG-only until S3 shorts accumulate",
            "exhaustion signals only fire while a position is open; signals after the real close are unobservable",
            "no multiplicity adjustment across the four signal arms",
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="/data", type=Path)
    p.add_argument("--shadow-book", type=Path, default=None)
    p.add_argument("--counterfactual", type=Path, default=None)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    out = args.output.resolve()
    if out.parent != Path("/tmp"):
        p.error("--output must be a file directly under /tmp")
    result = evaluate(args.data_dir, args.shadow_book, args.counterfactual)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(tmp, out)
    summary = {s: {"n": a["n"], "decision": a["decision"]}
               for s, a in result["arms"].items()}
    print(json.dumps({"output": str(out), "arms": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
