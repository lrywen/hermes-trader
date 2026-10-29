#!/usr/bin/env python3
"""Assemble a promotion-gate-ready veto evidence file for an M2 arm.

Merges the OFFLINE veto evidence (V1-V5: blocked cohort stats, gross, kept-minus
-blocked, concentration) with the LIVE real-verdict bridge output (V6). The
offline block is fixed once validated; the real_verdict block is refreshed from
real_verdict_eval each time. Output feeds promotion_gate.py --evidence directly.

Usage:
  python3 assemble_veto_evidence.py \
      --offline analysis/m2_r1_veto_offline.json \
      --real /data/m2_real_verdict.json \
      --rule mover_bypass_veto --out /data/m2_r1_evidence.json
"""
import argparse, json, sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", required=True)
    ap.add_argument("--real", required=True)
    ap.add_argument("--rule", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    off = json.load(open(a.offline))
    real = json.load(open(a.real))
    rv = None
    for arm in real.get("arms", []):
        if arm.get("rule") == a.rule:
            rv = arm.get("real_verdict")
            break
    if rv is None:
        print(f"ERROR: rule {a.rule} not found in real verdict file", file=sys.stderr)
        return 2
    ev = dict(off)
    ev["kind"] = "veto"
    ev["real_verdict"] = {"n": rv.get("n", 0), "agree": rv.get("agree"),
                          "same_direction": rv.get("same_direction"),
                          "mean_pct": rv.get("mean_pct")}
    text = json.dumps(ev, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
