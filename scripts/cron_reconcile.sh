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
  echo
  exit "$rc"
} >> "$LOG_FILE" 2>&1
