#!/usr/bin/env bash
# Daily reconciliation cron wrapper — runs inside the hermes-trader container.
#
# Compares Hyperliquid userFills against local memory trade/close records and
# pushes a Feishu alert (category=risk) on any orphan open / orphan close /
# phantom close. Intended to be invoked from the host crontab once a day, e.g.
# at 00:15 UTC (08:15 Asia/Shanghai):
#
#   15 8 * * * /home/ldy/hermes-trader/scripts/cron_reconcile.sh
#
# Exit codes (from reconcile_fills.py):
#   0  clean
#   2  discrepancies found (alert already pushed)
#   3  API / configuration error
set -u

CONTAINER="${HERMES_CONTAINER:-hermes-trader}"
LOG_DIR="${HERMES_RECONCILE_LOG_DIR:-/home/ldy/.local/state/hermes-trader}"
LOG_FILE="$LOG_DIR/reconcile.log"
WINDOW_HOURS="${HERMES_RECONCILE_WINDOW:-26}"

mkdir -p "$LOG_DIR"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

{
  echo "===== $(ts) reconcile start (window=${WINDOW_HOURS}h) ====="
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "$(ts) ERROR: container '$CONTAINER' not running"
    # Best-effort direct Feishu-free failure signal: log only.
    exit 3
  fi

  docker exec "$CONTAINER" python /app/scripts/reconcile_fills.py \
    --window-hours "$WINDOW_HOURS" --alert-on-orphan
  rc=$?
  echo "$(ts) reconcile exit=$rc"

  # Audit 2026-09-08 (change-arm counterfactual backfill): backfill the three
  # "change" shadow arms (sizing_v2 / atr_regime_calib / confidence_decay) with
  # counterfactual outcomes so the 00:45 UTC grader (shadow_grade) has mature
  # outcomes + pnl to judge instead of the perpetual "no backfill" weak signal.
  # Pure paper: never places orders or writes config. Best-effort: shadow
  # backfill must never mask or alter the fills reconcile exit code above.
  echo "$(ts) change-arm shadow backfill start (window=${HERMES_CHANGE_ARM_WINDOW:-30}h)"
  docker exec "$CONTAINER" python /app/scripts/reconcile_change_arms_shadow.py \
    --window-hours "${HERMES_CHANGE_ARM_WINDOW:-30}" --write
  echo "$(ts) change-arm shadow backfill exit=$?"

  # Audit 2026-09-10 (M12 fix): pullback-long shadow backfill. This arm's
  # reconcile script existed but was NEVER scheduled, and its default path
  # (~/.hermes-trading) is read-only in-container, so production records live in
  # /data and all 42 historical signals stayed outcome-less forever. Pass the
  # /data path explicitly and let the script merge daily rotations .1..5. Mature
  # window 48h (pullback outcomes need two+ days of 1h bars). Pure paper, best-
  # effort: never masks the fills reconcile exit code above.
  echo "$(ts) pullback shadow backfill start (window=${HERMES_PULLBACK_WINDOW:-48}h)"
  docker exec "$CONTAINER" python /app/scripts/reconcile_pullback_shadow.py \
    --file "${HERMES_PULLBACK_SHADOW_FILE:-/data/pullback_shadow.jsonl}" \
    --window-hours "${HERMES_PULLBACK_WINDOW:-48}" --write
  echo "$(ts) pullback shadow backfill exit=$?"

  # D2 (2026-10-23): ignite_grace_cf outcome join. The script existed but was
  # never in the image nor scheduled, so ignite_grace_cf rows had no joined
  # exit and the n>=30 promotion gate could never be read. Reads the same
  # /data file the container writes. Pure paper; best-effort like the above.
  echo "$(ts) risk_tuning shadow reconcile start (window=${HERMES_RISK_TUNING_WINDOW:-48}h)"
  docker exec "$CONTAINER" python /app/scripts/reconcile_risk_tuning_shadow.py \
    --file "${HERMES_RISK_TUNING_SHADOW_FILE:-/data/risk_tuning_shadow.jsonl}" \
    --window-hours "${HERMES_RISK_TUNING_WINDOW:-48}" --write
  echo "$(ts) risk_tuning shadow reconcile exit=$?"

  # Audit 2026-09-11 (xs_reversal outcome gap): this arm's reconcile script
  # existed but was NEVER scheduled, so all shadow rows stayed outcome-less and
  # the 00:45 grader reported "outcome 回填 0/44". The script also emitted the
  # legacy vocabulary "winner"/"loser", which the grader does not count (it only
  # counts win/loss); both are fixed together. No --max-age-hours: grade every
  # rotated row; grade() itself skips signals whose 72h bar has not printed
  # (immature). Pure paper, best-effort: never masks the fills reconcile exit.
  echo "$(ts) xs_reversal shadow backfill start"
  docker exec "$CONTAINER" python /app/scripts/reconcile_xs_reversal_shadow.py \
    --file "${HERMES_XS_REVERSAL_SHADOW_FILE:-/data/xs_reversal_shadow.jsonl}" \
    --write
  echo "$(ts) xs_reversal shadow backfill exit=$?"

  # Audit 2026-09-11 (daily_extension_cap outcome gap): this anti-chase long
  # gate had NO reconcile script at all, and its probe records carry no
  # entry_px. The new script reconstructs the would-be chase entry as the
  # signal-bar 1h close and grades a 72h forward long return (win/loss),
  # bucketing ext_would_block true/false. Long-only; data_missing rows skipped.
  # Pure paper, best-effort: never masks the fills reconcile exit code above.
  echo "$(ts) daily_extension_cap shadow backfill start"
  docker exec "$CONTAINER" python /app/scripts/reconcile_daily_extension_cap_shadow.py \
    --file "${HERMES_DAILY_EXTENSION_CAP_SHADOW_FILE:-/data/daily_extension_cap_shadow.jsonl}" \
    --write
  echo "$(ts) daily_extension_cap shadow backfill exit=$?"

  # Audit 2026-09-11 (relax_tier probe outcome backfill): the three stricter
  # trend-strength counterfactuals (rt_relax45 / rt_weak_rsi70 / rt_no_adx20)
  # are recorded on every PASSED ta_late decision in ta_late_entry_shadow.jsonl.
  # Replay 6h/24h/72h side-aware forward returns (net of round-trip fees) and
  # bucket flagged vs spared per arm so any future enforcement is data-backed.
  # Reads the live file plus rotated siblings; already-graded rows are skipped.
  # Pure paper, best-effort: never masks the fills reconcile exit code above.
  echo "$(ts) relax_tier shadow backfill start"
  docker exec "$CONTAINER" python /app/scripts/reconcile_relax_tier_shadow.py \
    --file "${HERMES_TA_LATE_ENTRY_SHADOW_FILE:-/data/ta_late_entry_shadow.jsonl}" \
    --write
  echo "$(ts) relax_tier shadow backfill exit=$?"

  # Audit 2026-10-23 (weight_shadow / stop_confirm outcome gap): both
  # reconcilers were shipped in the image but NEVER scheduled, so their shadow
  # rows were only ever read by hand. They read the arm files the perception /
  # dsl_exit writers place on the /data volume — NOT the bind-mounted
  # ~/.hermes-trading directory, which the container mounts read-only so the
  # writers could never append there. Paths are passed explicitly so a HOME
  # change cannot silently point the reconciliation at an empty directory.
  # Pure paper summaries; best-effort like the rest of this block.
  echo "$(ts) weight_shadow reconcile start (window=${HERMES_WEIGHT_SHADOW_WINDOW:-168}h)"
  docker exec "$CONTAINER" python /app/scripts/reconcile_weight_shadow.py \
    --file "${HERMES_WEIGHT_SHADOW_FILE:-/data/weight_shadow.jsonl}" \
    --window-hours "${HERMES_WEIGHT_SHADOW_WINDOW:-168}"
  echo "$(ts) weight_shadow reconcile exit=$?"

  echo "$(ts) stop_confirm shadow reconcile start (window=${HERMES_STOP_CONFIRM_WINDOW:-168}h)"
  docker exec "$CONTAINER" python /app/scripts/reconcile_stop_confirm_shadow.py \
    --file "${HERMES_STOP_CONFIRM_SHADOW_FILE:-/data/stop_confirm_shadow.jsonl}" \
    --window-hours "${HERMES_STOP_CONFIRM_WINDOW:-168}"
  echo "$(ts) stop_confirm shadow reconcile exit=$?"

  # M1 (2026-10-29 plan G1): universe-wide decision-shadow forward-return
  # reconciliation. Reads runner_gate_shadow.jsonl* (EVERY evaluated candidate,
  # admitted or blocked, incl. microcaps), aligns PIT 5m entry bars, attaches
  # 1h/4h/24h/72h net returns at 9.26bp and 19bp plus equal-weight market
  # excess, and buckets by path A/B/C, fired trigger and volume tier with
  # day-block bootstrap CI. Network candle fallback enabled. Read-only: never
  # mutates the source and never places orders. Best-effort like the arms above.
  echo "$(ts) decision_shadow reconcile start"
  docker exec "$CONTAINER" python /app/scripts/reconcile_decision_shadow.py     --pattern /data/runner_gate_shadow.jsonl*     --cache /data/ch_hist_candles.json     --out /data/decision_shadow_outcomes.jsonl     --summary-out /data/decision_shadow_summary.json
  echo "$(ts) decision_shadow reconcile exit=$?"

  echo
  exit "$rc"
} >> "$LOG_FILE" 2>&1
