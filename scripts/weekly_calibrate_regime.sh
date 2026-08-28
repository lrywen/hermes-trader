#!/usr/bin/env bash
# weekly_calibrate_regime.sh — rolling weekly recalibration of the production
# regime classifier, intended to run from cron every Monday 00:30 (Asia/Shanghai).
#
# What it does:
#   1. Runs scripts/calibrate_regime_thresholds.py over the trailing N days.
#   2. Archives the JSON grid + stdout log under ARCHIVE_DIR/YYYY-WW/.
#   3. Optionally (--apply) writes the BEST constants into the
#      `regime_classifier` block of .agent-config.json, but ONLY when the new
#      set improves cost vs the baseline by MIN_DELTA and does not regress
#      agreement. Without --apply it is a dry run that only reports/archives.
#   4. Rotates archives, keeping the latest KEEP_WEEKS weekly runs.
#
# Cron entry (crontab -e), Mondays 00:30 local time:
#   30 0 * * 1 /home/ldy/hermes-trader/scripts/weekly_calibrate_regime.sh --apply \
#       >> /var/log/hermes_regime_calib.log 2>&1
#
# Without --apply the script never touches .agent-config.json — safe to
# schedule first and review before enabling auto-promotion.

set -euo pipefail

REPO_DIR="/home/ldy/hermes-trader"
ARCHIVE_DIR="${ARCHIVE_DIR:-/var/lib/hermes-trader/regime_calib}"
DAYS="${DAYS:-30}"
COINS="${COINS:-20}"
KEEP_WEEKS="${KEEP_WEEKS:-12}"
# Auto-promotion guardrails (only used with --apply):
MIN_DELTA="${MIN_DELTA:-0.01}"     # cost must improve by >= 1.0 point
MAX_FALSE_TREND="${MAX_FALSE_TREND:-0.35}"
# Reject degenerate crosses where fast/slow are too close (e.g. 20/21 collapses
# the EMA cross into a pure slope detector). 20/30 (the vetted robust set) has
# gap=10; set to 5 to reject only near-equal periods.
MIN_EMA_GAP="${MIN_EMA_GAP:-5}"
APPLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --days) DAYS="$2"; shift 2 ;;
        --coins) COINS="$2"; shift 2 ;;
        --archive-dir) ARCHIVE_DIR="$2"; shift 2 ;;
        --keep) KEEP_WEEKS="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

cd "$REPO_DIR"

# ISO week stamp, e.g. 2026-W34 (stable across the whole week).
WEEK_STAMP="$(date +%G-W%V)"
RUN_TS="$(date +%Y%m%dT%H%M%S)"
RUN_DIR="$ARCHIVE_DIR/$WEEK_STAMP"
mkdir -p "$RUN_DIR"

JSON_OUT="$RUN_DIR/calib_${RUN_TS}.json"
LOG_OUT="$RUN_DIR/calib_${RUN_TS}.log"
LATEST_LINK="$ARCHIVE_DIR/latest.json"

echo "[$(date -Iseconds)] === weekly regime calibration start ==="
echo "[$(date -Iseconds)] week=$WEEK_STAMP days=$DAYS coins=$COINS apply=$APPLY"
echo "[$(date -Iseconds)] archive=$RUN_DIR"

# 1. Run the grid search. Tee stdout to the log and capture exit status.
set +e
python3 scripts/calibrate_regime_thresholds.py \
    --days "$DAYS" --coins "$COINS" \
    --write-json "$JSON_OUT" 2>&1 | tee "$LOG_OUT"
CALIB_RC=${PIPESTATUS[0]}
set -e

if [[ $CALIB_RC -ne 0 ]]; then
    echo "[$(date -Iseconds)] ERROR: calibration failed (rc=$CALIB_RC) — leaving config untouched" >&2
    exit $CALIB_RC
fi

# Refresh the convenience symlink to the newest JSON.
ln -sfn "$JSON_OUT" "$LATEST_LINK"

# 2. Decide whether to promote the new constants into .agent-config.json.
#    The python snippet reads baseline/best from the JSON and applies the
#    guardrails; it prints a single machine-readable verdict line plus a
#    human-readable summary.
PROMOTE_OUT="$(python3 - "$JSON_OUT" "$MIN_DELTA" "$MAX_FALSE_TREND" "$MIN_EMA_GAP" "$APPLY" <<'PYEOF'
import json, sys

path, min_delta, max_false, min_gap, apply = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"
data = json.load(open(path))
base, best = data["baseline"], data["best"]

delta_cost = base["cost"] - best["cost"]           # >0 means improvement
delta_agree = best["agree"] - base["agree"]       # >0 means improvement

reasons = []
if best["fast"] >= best["slow"]:
    reasons.append("fast>=slow")
if (best["slow"] - best["fast"]) < min_gap:
    reasons.append(f"EMA gap {best['slow']-best['fast']} < {min_gap} (degenerate cross)")
if delta_cost < min_delta:
    reasons.append(f"cost improvement {delta_cost*100:.2f}pts < {min_delta*100:.2f}")
if delta_agree < 0:
    reasons.append(f"agreement regresses {delta_agree*100:+.2f}pts")
if best["false_trend"] > max_false:
    reasons.append(f"false_trend {best['false_trend']*100:.1f}% > {max_false*100:.1f}%")

verdict = "PROMOTE" if (apply and not reasons) else ("DRY_RUN" if not apply else "REJECT")
print(f"VERDICT={verdict}")
print(f"BASELINE cost={base['cost']*100:.2f}% agree={base['agree']*100:.1f}% "
      f"false={base['false_trend']*100:.1f}% miss={base['missed_trend']*100:.1f}% "
      f"params=({base['fast']},{base['slow']},{base['slope']:.4f},{base['adx']:.0f})")
print(f"BEST     cost={best['cost']*100:.2f}% agree={best['agree']*100:.1f}% "
      f"false={best['false_trend']*100:.1f}% miss={best['missed_trend']*100:.1f}% "
      f"params=({best['fast']},{best['slow']},{best['slope']:.4f},{best['adx']:.0f})")
print(f"DELTA    cost={delta_cost*100:+.2f}pts agree={delta_agree*100:+.2f}pts")
if reasons:
    print("REASONS=" + "; ".join(reasons))
if verdict == "PROMOTE":
    print("FAST=%d" % best["fast"])
    print("SLOW=%d" % best["slow"])
    print("SLOPE=%.6f" % best["slope"])
    print("ADX=%.2f" % best["adx"])
PYEOF
)"

echo "$PROMOTE_OUT"
echo "[$(date -Iseconds)] log saved: $LOG_OUT"
echo "[$(date -Iseconds)] json saved: $JSON_OUT"

VERDICT="$(echo "$PROMOTE_OUT" | sed -n 's/^VERDICT=//p')"

if [[ "$VERDICT" == "PROMOTE" ]]; then
    FAST="$(echo "$PROMOTE_OUT"   | sed -n 's/^FAST=//p')"
    SLOW="$(echo "$PROMOTE_OUT"   | sed -n 's/^SLOW=//p')"
    SLOPE="$(echo "$PROMOTE_OUT"  | sed -n 's/^SLOPE=//p')"
    ADX="$(echo "$PROMOTE_OUT"    | sed -n 's/^ADX=//p')"

    cp .agent-config.json "$RUN_DIR/agent_config_before.json"
    python3 - "$FAST" "$SLOW" "$SLOPE" "$ADX" <<'PYEOF'
import sys
sys.path.insert(0, ".")
from hermes_trader.agents.config_store import read_agent_config, write_agent_config
fast, slow, slope, adx = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
cfg = read_agent_config()
cfg["regime_classifier"] = {
    "fast_ema": fast, "slow_ema": slow,
    "slope_threshold": slope, "chop_adx_max": adx,
}
write_agent_config(cfg)
print(f"[config] regime_classifier updated -> fast={fast} slow={slow} slope={slope} adx<{adx}")
PYEOF
    cp .agent-config.json "$RUN_DIR/agent_config_after.json"
    echo "[$(date -Iseconds)] config promoted and snapshotted in $RUN_DIR"
elif [[ "$VERDICT" == "REJECT" ]]; then
    echo "[$(date -Iseconds)] best params rejected by guardrails — config unchanged" >&2
else
    echo "[$(date -Iseconds)] dry-run (no --apply) — config unchanged; review $JSON_OUT"
fi

# 3. Rotate: keep only the newest KEEP_WEEKS week directories.
mapfile -t OLD_WEEKS < <(ls -1d "$ARCHIVE_DIR"/*-W* 2>/dev/null | sort -r | tail -n +$((KEEP_WEEKS + 1)) || true)
for d in "${OLD_WEEKS[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf "$d" && echo "[$(date -Iseconds)] rotated old archive: $d"
done

echo "[$(date -Iseconds)] === weekly regime calibration done (verdict=$VERDICT) ==="
