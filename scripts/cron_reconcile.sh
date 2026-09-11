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

  echo
  exit "$rc"
} >> "$LOG_FILE" 2>&1
