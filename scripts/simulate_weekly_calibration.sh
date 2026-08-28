#!/usr/bin/env bash
# simulate_weekly_calibration.sh — sandbox dry-run of the weekly recalibration.
#
# Runs calibrate_regime_thresholds.py over the trailing DAYS (default 60),
# archives into a SANDBOX dir, and reports what the weekly cron WOULD do if
# invoked with --apply, WITHOUT touching .agent-config.json.
#
# It also scans the full ranked grid for parameter sets whose EMA gap is near
# the MIN_EMA_GAP guardrail (gap 4/5/6 by default), so we can see whether the
# calibration is producing borderline crosses that the guardrail would reject.
#
# Usage:
#   bash scripts/simulate_weekly_calibration.sh [--days 60] [--coins 20]
#
# Exit code: 0 if --apply WOULD promote (guardrails pass), 1 if it would be
# rejected, 2 on calibration/runtime failure.

set -euo pipefail

REPO_DIR="/home/ldy/hermes-trader"
DAYS=60
COINS=20
# Guardrails — mirror weekly_calibrate_regime.sh defaults.
MIN_DELTA=0.01
MAX_FALSE_TREND=0.35
MIN_EMA_GAP=5
# Report grid rows whose gap is within this band around MIN_EMA_GAP.
GAP_BAND=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --days) DAYS="$2"; shift 2 ;;
        --coins) COINS="$2"; shift 2 ;;
        --min-delta) MIN_DELTA="$2"; shift 2 ;;
        --max-false) MAX_FALSE_TREND="$2"; shift 2 ;;
        --min-gap) MIN_EMA_GAP="$2"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

cd "$REPO_DIR"

SANDBOX="/tmp/regime_calib_sim/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$SANDBOX"
JSON_OUT="$SANDBOX/calib.json"
LOG_OUT="$SANDBOX/calib.log"

echo "=== weekly calibration simulation (SANDBOX, config untouched) ==="
echo "days=$DAYS coins=$COINS sandbox=$SANDBOX"
echo "guardrails: min_delta=$MIN_DELTA max_false=$MAX_FALSE_TREND min_ema_gap=$MIN_EMA_GAP"
echo

set +e
python3 scripts/calibrate_regime_thresholds.py \
    --days "$DAYS" --coins "$COINS" \
    --write-json "$JSON_OUT" 2>&1 | tee "$LOG_OUT"
CALIB_RC=${PIPESTATUS[0]}
set -e

if [[ $CALIB_RC -ne 0 ]]; then
    echo
    echo "SIM RESULT: RUNTIME_FAILURE (rc=$CALIB_RC) — see $LOG_OUT"
    exit 2
fi

echo
echo "=== guardrail evaluation (what --apply WOULD do) ==="

python3 - "$JSON_OUT" "$MIN_DELTA" "$MAX_FALSE_TREND" "$MIN_EMA_GAP" "$GAP_BAND" <<'PYEOF'
import json, sys

path, min_delta, max_false, min_gap, band = (
    sys.argv[1], float(sys.argv[2]), float(sys.argv[3]),
    int(sys.argv[4]), int(sys.argv[5]),
)
data = json.load(open(path))
base, best, top = data["baseline"], data["best"], data["top"]

delta_cost = base["cost"] - best["cost"]
delta_agree = best["agree"] - base["agree"]

reasons = []
if best["fast"] >= best["slow"]:
    reasons.append("fast>=slow")
gap = best["slow"] - best["fast"]
if gap < min_gap:
    reasons.append(f"EMA gap {gap} < {min_gap} (degenerate cross)")
if delta_cost < min_delta:
    reasons.append(f"cost improvement {delta_cost*100:.2f}pts < {min_delta*100:.2f}")
if delta_agree < 0:
    reasons.append(f"agreement regresses {delta_agree*100:+.2f}pts")
if best["false_trend"] > max_false:
    reasons.append(f"false_trend {best['false_trend']*100:.1f}% > {max_false*100:.1f}%")

verdict = "PASS (would PROMOTE)" if not reasons else "FAIL (would REJECT)"
print(f"best params: fast={best['fast']} slow={best['slow']} gap={gap} "
      f"slope={best['slope']:.4f} adx={best['adx']:.0f}")
print(f"baseline:    cost={base['cost']*100:.2f}% agree={base['agree']*100:.1f}% "
      f"false={base['false_trend']*100:.1f}% miss={base['missed_trend']*100:.1f}%")
print(f"best:        cost={best['cost']*100:.2f}% agree={best['agree']*100:.1f}% "
      f"false={best['false_trend']*100:.1f}% miss={best['missed_trend']*100:.1f}%")
print(f"delta:       cost={delta_cost*100:+.2f}pts agree={delta_agree*100:+.2f}pts")
print(f"SIM VERDICT: {verdict}")
if reasons:
    print("  reasons:")
    for r in reasons:
        print(f"    - {r}")

# Borderline-gap scan across the top-N grid: how many results sit at gap
# within [min_gap-band, min_gap+band], and what is the best cost among rows
# that PASS the gap guardrail.
lo, hi = min_gap - band, min_gap + band
borderline = [r for r in top if lo <= (r["slow"] - r["fast"]) <= hi]
passing = [r for r in top
           if (r["slow"] - r["fast"]) >= min_gap
           and (base["cost"] - r["cost"]) >= min_delta
           and r["agree"] >= base["agree"]
           and r["false_trend"] <= max_false]

print()
print(f"borderline gap scan (gap in [{lo},{hi}] among top {len(top)}): "
      f"{len(borderline)} row(s)")
for r in borderline:
    tag = "REJECT-gap" if (r["slow"]-r["fast"]) < min_gap else "pass-gap"
    print(f"  [{tag}] fast={r['fast']} slow={r['slow']} gap={r['slow']-r['fast']} "
          f"slope={r['slope']:.4f} adx={r['adx']:.0f} "
          f"cost={r['cost']*100:.2f}% agree={r['agree']*100:.1f}% "
          f"false={r['false_trend']*100:.1f}%")

if passing:
    p = passing[0]
    print()
    print(f"best PASSING-grid row: fast={p['fast']} slow={p['slow']} "
          f"gap={p['slow']-p['fast']} slope={p['slope']:.4f} adx={p['adx']:.0f} "
          f"cost={p['cost']*100:.2f}% agree={p['agree']*100:.1f}% "
          f"false={p['false_trend']*100:.1f}%")
else:
    print()
    print("best PASSING-grid row: NONE — no top-N row satisfies all guardrails")

sys.exit(0 if not reasons else 1)
PYEOF
PY_RC=$?

echo
echo "sandbox JSON: $JSON_OUT"
echo "sandbox log:  $LOG_OUT"
if [[ $PY_RC -eq 0 ]]; then
    echo "SIM RESULT: PASS — --apply would promote the best params."
else
    echo "SIM RESULT: FAIL — --apply would reject; review borderline rows above."
fi
exit $PY_RC
