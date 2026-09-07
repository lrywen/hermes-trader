#!/usr/bin/env bash
# Nightly shadow-arm grading cron wrapper — runs inside the hermes-trader
# container.
#
# Audit 2026-09-07 (Pathia nightly rater absorption): this drives the INERT
# shadow_grade.py rater once a night. It grades every shadow/enforce risk arm
# over 24/72/168h windows from the forward shadow ledgers and pushes a Feishu
# risk card ONLY when something needs human attention (DATA_GAP blind gate /
# REVIEW suspected harm / PROMOTE candidate). It never writes config, never
# places orders, never changes gate state — promotions still go through
# config_store + human sign-off.
#
# Intended to be invoked from the host crontab once a day, e.g. 00:45 CST
# (just after the 00:15 reconcile):
#
#   45 8 * * * /home/ldy/hermes-trader/scripts/cron_shadow_grade.sh
#
# Exit codes (from shadow_grade.py):
#   0  no blind-gate DATA_GAP (ratings themselves never block anything)
#   1  at least one configured arm produced zero records (alert already pushed)
set -u

CONTAINER="${HERMES_CONTAINER:-hermes-trader}"
LOG_DIR="${HERMES_GRADE_LOG_DIR:-/home/ldy/.local/state/hermes-trader}"
LOG_FILE="$LOG_DIR/shadow_grade.log"
WINDOWS="${HERMES_GRADE_WINDOWS:-24 72 168}"

mkdir -p "$LOG_DIR"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

{
  echo "===== $(ts) shadow_grade start (windows=${WINDOWS}) ====="
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "$(ts) ERROR: container '$CONTAINER' not running"
    # Best-effort failure signal: log only (no Feishu from outside the container).
    exit 3
  fi

  # --push sends the risk card (best-effort, never raises); the grader is
  # read-only regardless. Word-splitting WINDOWS is intentional.
  docker exec "$CONTAINER" python /app/scripts/shadow_grade.py --push --windows $WINDOWS
  rc=$?
  echo "$(ts) shadow_grade exit=$rc"
  echo
  exit "$rc"
} >> "$LOG_FILE" 2>&1
