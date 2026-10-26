#!/usr/bin/env python3
"""Stage 0 hypothesis audit — run the three iron-law gates and log one Trial.

This is the missing "library -> process" glue for the 2026-10-25 Stage 0
framework. A candidate feature is promoted out of Stage 0 ONLY if it clears, on
one frozen long-form dataset:

  1. fixed_effect_ic      — not a pure cross-sectional scale factor
                            (within-coin IC must stay a meaningful fraction of
                            the pooled IC and not flip sign);
  2. incremental value    — partial rank-IC survives the known control factors
                            AND within-coin IC is same-sign and material in
                            every volatility bucket (ic_by_volatility_group);
  3. portfolio tradability — net bps after full round-trip cost for daily
                            top-N selection is positive for N=1..5 with a
                            day-block bootstrap CI above zero and no N=1/N=5
                            sign opposition.

Every successful gate run appends exactly one Trial line to an append-only
ledger, stamping the running family size m so the Bonferroni correction
(m * p_raw) cannot be rewritten after the fact. A run that cannot evaluate the
gates (missing input or a missing coin identity on any row) appends NOTHING and
exits rc=2/rc=3 — an un-runnable audit must never record a verdict it did not
compute. This is the first real caller of TrialLedger and the first on-disk
research family; it has NO trading side effects and does not read or write the
live /data state.

Input: CSV or JSONL with one row per (day, coin) observation and at least the
columns: day, coin, <feature>, <ret>. Optional: <vol> for volatility bucketing
and one or more --control feature columns. Forward returns are FRACTIONS
(0.001 == 10 bps). Exactly one row per (day, coin) is required for the
portfolio gate; duplicates are aggregated with an explicit --dedup policy
(last/mean) before that gate rather than silently first-wins.

Exit codes (machine-readable for cron/CI):
  0 = hypothesis cleared every available gate (reject H0)
  1 = gates ran and rejected it (fail to reject H0 / not tradable)
  2 = invalid input / insufficient data (no verdict possible)
  3 = gate precondition error (e.g. missing coin identity)

The ledger path defaults to $HERMES_STAGE0_LEDGER or
~/.hermes-trading/stage0_trials.jsonl on the host; point --ledger at a tmp file
in tests. Nothing here is imported by the trading runtime.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import asdict

from hermes_trader.validation import (
    Trial,
    TrialLedger,
    fixed_effect_ic,
    ic_by_volatility_group,
    partial_rank_ic,
    simulate_sizes,
    tradability_verdict,
)

DEFAULT_FAMILY = "stage0"


def _load_rows(path: str) -> list[dict]:
    """Read a CSV or JSONL file into numeric long-form rows."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    rows: list[dict] = []
    if path.endswith(".jsonl"):
        with open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    else:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for rec in csv.DictReader(fh):
                rows.append(rec)
    return rows


def _coerce_numeric(rows: list[dict], keys: list[str]) -> list[dict]:
    """Parse the numeric gate columns to float; drop rows missing any of them."""
    out: list[dict] = []
    for r in rows:
        try:
            parsed = dict(r)
            for k in keys:
                if k in ("day", "coin"):
                    continue
                v = r.get(k)
                if v is None or v == "":
                    raise ValueError(k)
                parsed[k] = float(v)
        except (TypeError, ValueError):
            continue
        out.append(parsed)
    return out


def _dedup_day_coin(
    rows: list[dict], feature: str, ret: str,
    day_key: str, coin_key: str, policy: str,
    extra_num: list[str],
) -> list[dict]:
    """Collapse to exactly one row per (day, coin) with an explicit policy."""
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        buckets.setdefault((r[day_key], r[coin_key]), []).append(r)
    out: list[dict] = []
    num_cols = [feature, ret] + extra_num
    for (d, c), grp in buckets.items():
        if len(grp) == 1:
            out.append(grp[0])
            continue
        if policy == "last":
            out.append(grp[-1])
            continue
        base = dict(grp[0])
        for k in num_cols:
            vals = [g[k] for g in grp if isinstance(g.get(k), (int, float))
                    and math.isfinite(g[k])]
            if vals:
                base[k] = sum(vals) / len(vals)
        out.append(base)
    return out


def _leave_one_coin_worst(
    rows: list[dict], feature: str, ret: str, coin_key: str,
) -> float:
    """Worst single-coin mean forward bps among the feature-positive picks."""
    per_coin: dict[str, list[float]] = {}
    for r in rows:
        if r[feature] > 0:
            per_coin.setdefault(r[coin_key], []).append(r[ret] * 1e4)
    means = [sum(v) / len(v) for v in per_coin.values() if v]
    return min(means) if means else float("nan")


def run_audit(args: argparse.Namespace) -> tuple[str, dict]:
    raw = _load_rows(args.input)
    need = [args.feature, args.ret] + args.control
    if args.vol:
        need.append(args.vol)
    rows = _coerce_numeric(raw, need)
    n_coins = len({r.get(args.coin) for r in rows})
    if len(rows) < args.min_rows or n_coins < 2:
        return ("invalid", {"reason": f"need >= {args.min_rows} finite rows and "
                                      f">=2 coins; got n={len(rows)} coins={n_coins}"})

    gates: dict[str, object] = {}
    failures: list[str] = []

    # Gate 1 — fixed-effect / scale factor.
    fe = fixed_effect_ic(rows, args.feature, args.ret, coin_key=args.coin,
                         ratio_threshold=args.ratio_threshold)
    gates["fixed_effect"] = asdict(fe)
    if fe.is_scale_factor:
        failures.append(f"scale_factor:{fe.reason}")

    # Gate 2a — incremental value over known controls.
    if args.control:
        try:
            pi = partial_rank_ic(rows, args.feature, args.ret,
                                 control_keys=args.control,
                                 coin_key=args.coin)
            gates["partial_ic"] = asdict(pi)
            if not pi.estimable or pi.absorbed:
                failures.append(f"absorbed:{pi.reason}")
        except ValueError as exc:  # rank-deficient controls etc.
            gates["partial_ic"] = {"error": str(exc)}
            failures.append(f"partial_ic_error:{exc}")

    # Gate 2b — robustness across volatility regimes (only if vol is supplied).
    if args.vol:
        vg = ic_by_volatility_group(
            rows, args.feature, args.ret, args.vol,
            coin_key=args.coin, n_groups=3,
            min_per_group=args.min_per_group, min_abs_ic=args.min_abs_ic,
            significance_z=args.significance_z)
        gates["vol_group"] = asdict(vg)
        if not vg.consistent:
            failures.append("vol_group_not_consistent")

    # Gate 3 — daily cross-sectional tradability (needs a day key).
    tradable = False
    if not args.no_portfolio:
        extra = ([args.vol] if args.vol else []) + args.control
        deduped = _dedup_day_coin(rows, args.feature, args.ret,
                                  args.day, args.coin, args.dedup, extra)
        sizes = simulate_sizes(deduped, args.feature, args.ret,
                               sizes=tuple(args.sizes),
                               cost_bps=args.cost_bps)
        gates["portfolio"] = {str(n): asdict(r) for n, r in sizes.items()}
        # Drop the bulky per-day map from the ledger record.
        for rec in gates["portfolio"].values():
            rec.pop("per_day_net", None)
        ok, why = tradability_verdict(sizes)
        gates["tradability"] = {"ok": ok, "why": why}
        if not ok:
            failures.append(f"not_tradable:{why}")
        tradable = ok
        headline = sizes[max(sizes)].net_bps
        ci_lo = sizes[max(sizes)].net_ci_low
        ci_hi = sizes[max(sizes)].net_ci_high
        conc = sizes[max(sizes)].top1_concentration
        loc_worst = _leave_one_coin_worst(deduped, args.feature, args.ret,
                                          args.coin)
    else:
        headline = float("nan")
        ci_lo = ci_hi = conc = loc_worst = float("nan")

    verdict = "reject_h0" if not failures else "fail_reject"
    # A non-portfolio run only earns a tradable pass when explicitly requested;
    # treat absent gates as inconclusive unless the signal gates all clear.
    if args.no_portfolio and not failures:
        verdict = "inconclusive"

    caliber = (f"feature={args.feature};ret={args.ret};vol={args.vol or '-'};"
               f"controls={'|'.join(args.control) or '-'};"
               f"cost_bps={args.cost_bps};sizes={','.join(map(str, args.sizes))};"
               f"dedup={args.dedup};volgroup_floor=max(min_abs_ic={args.min_abs_ic}"
               f",tanh({args.significance_z}*1.0637/sqrt(n-3)))")
    trial = Trial(
        hypothesis=args.hypothesis,
        caliber=caliber,
        n=len(rows),
        net_bps=round(headline, 4),
        leave_one_coin_worst_bps=round(loc_worst, 4),
        ci_low_bps=round(ci_lo, 4),
        ci_high_bps=round(ci_hi, 4),
        top1_concentration=round(conc, 4) if math.isfinite(conc) else conc,
        family=args.family,
        verdict=verdict,
        notes=(";".join(failures) or ("tradable" if tradable else "signal_only")),
    )
    return verdict, {"gates": gates, "trial_preview": asdict(trial)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="CSV or JSONL long-form file")
    ap.add_argument("--hypothesis", required=True, help="one-line H1 statement")
    ap.add_argument("--feature", required=True)
    ap.add_argument("--ret", required=True, help="forward return, FRACTION")
    ap.add_argument("--vol", default="", help="optional volatility column")
    ap.add_argument("--control", action="append", default=[],
                    help="control feature column (repeatable)")
    ap.add_argument("--day", default="day")
    ap.add_argument("--coin", default="coin")
    ap.add_argument("--family", default=DEFAULT_FAMILY)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1, 2, 3, 5])
    ap.add_argument("--cost-bps", type=float, default=9.47)
    ap.add_argument("--dedup", choices=("mean", "last"), default="mean")
    ap.add_argument("--min-rows", type=int, default=120)
    ap.add_argument("--min-per-group", type=int, default=20)
    ap.add_argument("--min-abs-ic", type=float, default=0.10)
    ap.add_argument("--significance-z", type=float, default=1.96,
                    help="z s.d. for the sample-size-aware per-bucket Spearman "
                         "floor; the bucket floor is max(min-abs-ic, "
                         "tanh(z*1.0637/sqrt(n-3)))")
    ap.add_argument("--ratio-threshold", type=float, default=0.3)
    ap.add_argument("--no-portfolio", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="run gates but do not append to the ledger")
    ap.add_argument("--ledger",
                    default=os.environ.get(
                        "HERMES_STAGE0_LEDGER",
                        os.path.expanduser("~/.hermes-trading/stage0_trials.jsonl")))
    args = ap.parse_args(argv)

    try:
        verdict, detail = run_audit(args)
    except FileNotFoundError:
        print(json.dumps({"verdict": "invalid", "error": "input not found"}),
              file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        # Gate precondition failure (e.g. missing coin identity on some rows).
        # This is NOT a hypothesis rejection: map to rc=3 so cron/CI never
        # confuses "bad/incomplete data" with "the gates ran and falsified H0".
        # No Trial is appended — an un-runnable audit must not pollute the
        # ledger with a verdict it never actually computed.
        print(json.dumps({"verdict": "invalid", "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 3

    if verdict == "invalid":
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 2

    trial: Trial = Trial(**{k: v for k, v in detail["trial_preview"].items()})
    if not args.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(args.ledger)), exist_ok=True)
        rec = TrialLedger(args.ledger).append(trial)
    else:
        rec = detail["trial_preview"]

    out = {"verdict": verdict, "failures": detail["gates"].get("tradability"),
           "trial_id": rec.get("trial_id"), "family_size": rec.get("family_size_at_test"),
           "net_bps": rec.get("net_bps"), "ci": [rec.get("ci_low_bps"),
                                                 rec.get("ci_high_bps")],
           "gates": detail["gates"]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if verdict == "reject_h0" else 1


if __name__ == "__main__":
    raise SystemExit(main())
