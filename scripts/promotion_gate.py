#!/usr/bin/env python3
"""M2 promotion gate (offline, deterministic, fail-closed).

A candidate entry-selection ARM may only be promoted from SHADOW to a live
enforcement proposal when ALL six evidence conditions hold. This script never
enables anything: it reads an arm-evidence JSON and prints/writes a per-rule
verdict. A human must still approve the live config flip separately.

Evidence input (per arm), produced by arm_edge_eval + real-shadow recon:
{
  "arm": "R1_remove_daily_mover",
  "kept": {"n":..., "days":..., "mean_pct":..., "ci95_pct":[lo,hi]},
  "removed": {"n":..., "days":..., "mean_pct":...},
  "kept_minus_blocked_pct": ...,
  "baseline_blocked": {"mean_pct":...},
  "cost9_kept_pct": ..., "cost19_kept_pct": ...,
  "max_coin_share_pct": ..., "max_day_share_pct": ...,
  "real_verdict": {"n":..., "agree": true/false/null, "mean_pct":...}
}

Six conditions:
  C1 sample adequacy      : kept.n >= MIN_N and kept.days >= MIN_DAYS
  C2 net edge positive    : kept.mean_pct > 0 and CI lower bound > 0
  C3 beats blocked base   : kept_minus_blocked_pct > EDGE_MARGIN
  C4 cost robust          : positive under BOTH 9.26bp and 19bp
  C5 not concentrated     : no single coin/day dominates (< MAX_SHARE)
  C6 real-verdict confirm : real shadow agrees in sign and same direction
                            (null/not-yet => FAIL CLOSED: not enough real data)
"""
import argparse, json, sys

MIN_N = 100
MIN_DAYS = 40
EDGE_MARGIN = 0.05          # percentage points above the blocked baseline
MAX_COIN_SHARE_PCT = 35.0
MAX_DAY_SHARE_PCT = 20.0
MIN_REAL_N = 50


def evaluate(e: dict) -> dict:
    kept = e.get("kept") or {}
    ci = kept.get("ci95_pct") or [None, None]
    checks = {}

    checks["C1_sample"] = {
        "pass": kept.get("n", 0) >= MIN_N and kept.get("days", 0) >= MIN_DAYS,
        "detail": f"n={kept.get('n')} (>={MIN_N}), days={kept.get('days')} (>={MIN_DAYS})",
    }
    mean = kept.get("mean_pct")
    checks["C2_positive_edge"] = {
        "pass": bool(mean is not None and mean > 0 and ci[0] is not None and ci[0] > 0),
        "detail": f"mean={mean}%, CI95={ci} (must be >0, lower>0)",
    }
    kbd = e.get("kept_minus_blocked_pct")
    checks["C3_beats_blocked"] = {
        "pass": bool(kbd is not None and kbd > EDGE_MARGIN),
        "detail": f"kept_minus_blocked={kbd}% (margin>{EDGE_MARGIN})",
    }
    c9, c19 = e.get("cost9_kept_pct"), e.get("cost19_kept_pct")
    checks["C4_cost_robust"] = {
        "pass": bool(c9 is not None and c19 is not None and c9 > 0 and c19 > 0),
        "detail": f"net@9.26bp={c9}%, net@19bp={c19}% (both>0)",
    }
    mc = e.get("max_coin_share_pct", 100.0)
    md = e.get("max_day_share_pct", 100.0)
    checks["C5_not_concentrated"] = {
        "pass": bool(mc < MAX_COIN_SHARE_PCT and md < MAX_DAY_SHARE_PCT),
        "detail": f"max_coin_share={mc}% (<{MAX_COIN_SHARE_PCT}), max_day_share={md}% (<{MAX_DAY_SHARE_PCT})",
    }
    rv = e.get("real_verdict") or {}
    n_real = rv.get("n", 0)
    agree = bool(rv.get("agree")) and bool(rv.get("same_direction"))
    checks["C6_real_confirm"] = {
        "pass": bool(n_real >= MIN_REAL_N and agree),
        "detail": f"real_n={n_real} (>={MIN_REAL_N}), agree={rv.get('agree')}, "
                  f"same_direction={rv.get('same_direction')} (fail closed if insufficient)",
    }

    passed = [k for k, v in checks.items() if v["pass"]]
    verdict = "PROMOTE_CANDIDATE" if len(passed) == len(checks) else "HOLD_SHADOW"
    return {"arm": e.get("arm"), "verdict": verdict,
            "passed": len(passed), "of": len(checks), "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True, help="JSON file (one arm or {arms:[...]})")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    data = json.load(open(a.evidence))
    arms = data.get("arms") if isinstance(data, dict) and "arms" in data else [data]
    results = [evaluate(e) for e in arms]
    payload = {"promotion_gate_version": "m2-v1", "results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w").write(text)
    print(text)
    # fail-closed shell contract: nonzero unless every arm promotes
    return 0 if all(r["verdict"] == "PROMOTE_CANDIDATE" for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
