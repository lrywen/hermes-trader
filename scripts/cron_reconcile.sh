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

  # Audit 2026-09-11 (early_breakout half-size lane backfill): grades the
  # observation-only first-leg volume-breakout counterfactuals (two-phase 1h
  # sim, tight ATR stop + trailing floor, half vs full size). The lane defaults
  # to shadow off so the file may be empty; an empty/no-file run is harmless.
  # --window-hours is a freshness guard: signals newer than it are left pending.
  # Pure paper, best-effort: never masks the fills reconcile exit code above.
  echo "$(ts) early_breakout shadow backfill start (freshness=${HERMES_EARLY_BREAKOUT_WINDOW:-1}h)"
  docker exec "$CONTAINER" python /app/scripts/reconcile_early_breakout_shadow.py \
    --file "${HERMES_EARLY_BREAKOUT_SHADOW_FILE:-/data/early_breakout_shadow.jsonl}" \
    --window-hours "${HERMES_EARLY_BREAKOUT_WINDOW:-1}" --write
  echo "$(ts) early_breakout shadow backfill exit=$?"

  echo
  exit "$rc"
} >> "$LOG_FILE" 2>&1
