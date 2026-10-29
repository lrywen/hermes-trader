#!/usr/bin/env python3
"""M2 promotion gate (offline, deterministic, fail-closed).

Two arm kinds:
  kind="edge" (default): an entry-selection arm that must manufacture positive
    net edge -> six C-conditions on the kept portfolio.
  kind="veto": a loss-avoidance veto. It is justified when the trades it BLOCKS
    are themselves significantly negative (even at zero fee), removal improves
    the kept portfolio, the cohort is not concentrated, and real verdicts agree.
    A veto does NOT need the kept portfolio to be positive (it removes harm;
    it need not create edge). Six V-conditions.

This script never enables anything; a human approves the live config flip.
C/V6 real-verdict confirm FAILS CLOSED when real evidence is insufficient.
"""
import argparse, json, sys

MIN_N = 100
MIN_DAYS = 40
EDGE_MARGIN = 0.05          # percentage points above the blocked baseline
MAX_COIN_SHARE_PCT = 35.0
MAX_DAY_SHARE_PCT = 20.0
MIN_REAL_N = 50


def _concentration(e, prefix):
    mc = e.get("max_coin_share_pct", 100.0)
    md = e.get("max_day_share_pct", 100.0)
    ok = bool(mc < MAX_COIN_SHARE_PCT and md < MAX_DAY_SHARE_PCT)
    return ok, f"max_coin_share={mc}% (<{MAX_COIN_SHARE_PCT}), max_day_share={md}% (<{MAX_DAY_SHARE_PCT})"


def _real_check(e):
    rv = e.get("real_verdict") or {}
    n_real = rv.get("n", 0)
    agree = bool(rv.get("agree")) and bool(rv.get("same_direction"))
    ok = bool(n_real >= MIN_REAL_N and agree)
    return ok, (f"real_n={n_real} (>={MIN_REAL_N}), agree={rv.get('agree')}, "
                f"same_direction={rv.get('same_direction')} (fail closed if insufficient)")


def evaluate_edge(e):
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
    ok, detail = _concentration(e, "C5")
    checks["C5_not_concentrated"] = {"pass": ok, "detail": detail}
    ok, detail = _real_check(e)
    checks["C6_real_confirm"] = {"pass": ok, "detail": detail}
    passed = [k for k, v in checks.items() if v["pass"]]
    verdict = "PROMOTE_CANDIDATE" if len(passed) == len(checks) else "HOLD_SHADOW"
    return {"arm": e.get("arm"), "kind": "edge", "verdict": verdict,
            "passed": len(passed), "of": len(checks), "checks": checks}


def evaluate_veto(e):
    """Loss-avoidance veto criteria (the blocked cohort, not kept edge)."""
    blocked = e.get("blocked") or {}
    bci = blocked.get("ci95_pct") or [None, None]
    checks = {}
    checks["V1_sample"] = {
        "pass": blocked.get("n", 0) >= MIN_N and blocked.get("days", 0) >= MIN_DAYS,
        "detail": f"blocked n={blocked.get('n')} (>={MIN_N}), days={blocked.get('days')} (>={MIN_DAYS})",
    }
    bm = blocked.get("mean_pct")
    checks["V2_blocked_negative"] = {
        "pass": bool(bm is not None and bm < 0 and bci[1] is not None and bci[1] < 0),
        "detail": f"blocked mean={bm}%, CI95 upper={bci[1]} (must be <0)",
    }
    kbd = e.get("kept_minus_blocked_pct")
    checks["V3_removal_improves"] = {
        "pass": bool(kbd is not None and kbd > EDGE_MARGIN),
        "detail": f"kept_minus_blocked={kbd}% (must be >{EDGE_MARGIN})",
    }
    bg = e.get("blocked_gross_pct")
    checks["V4_bad_even_gross"] = {
        "pass": bool(bg is not None and bg < 0),
        "detail": f"blocked mean at zero fee={bg}% (must be <0: bad selection, not cost)",
    }
    ok, detail = _concentration(e, "V5")
    checks["V5_not_concentrated"] = {"pass": ok, "detail": detail}
    ok, detail = _real_check(e)
    checks["V6_real_confirm"] = {"pass": ok, "detail": detail}
    passed = [k for k, v in checks.items() if v["pass"]]
    verdict = "PROMOTE_VETO_CANDIDATE" if len(passed) == len(checks) else "HOLD_SHADOW"
    return {"arm": e.get("arm"), "kind": "veto", "verdict": verdict,
            "passed": len(passed), "of": len(checks), "checks": checks}


def evaluate(e):
    if e.get("kind") == "veto":
        return evaluate_veto(e)
    return evaluate_edge(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True, help="JSON file (one arm or {arms:[...]})")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    data = json.load(open(a.evidence))
    arms = data.get("arms") if isinstance(data, dict) and "arms" in data else [data]
    results = [evaluate(e) for e in arms]
    payload = {"promotion_gate_version": "m2-v2", "results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w").write(text)
    print(text)
    promote = all(r["verdict"] in ("PROMOTE_CANDIDATE", "PROMOTE_VETO_CANDIDATE")
                  for r in results)
    return 0 if promote else 2


if __name__ == "__main__":
    sys.exit(main())
