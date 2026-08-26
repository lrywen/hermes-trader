#!/usr/bin/env bash
# Keep the calibration guardrail monitor alive for the 7-day observation window.
#
# Launches monitor_calibration_guardrails.py and restarts it if it exits
# unexpectedly. Every exit (normal or crash) is logged with timestamp, exit
# code and signal. The monitor itself has a --duration-hours cap, so a clean
# exit at the end of the window is treated as "done" and not restarted.
#
# Usage:
#   nohup bash scripts/watch_calibration_monitor.sh \
#       > ~/.local/state/hermes-trader/watchdog.log 2>&1 &
#   echo $! > ~/.local/state/hermes-trader/watchdog.pid
#
# Stop:
#   kill "$(cat ~/.local/state/hermes-trader/watchdog.pid)"   # watchdog
#   pkill -f monitor_calibration_guardrails.py               # child

set -u

REPO_DIR=/home/ldy/hermes-trader
STATE_DIR="$HOME/.local/state/hermes-trader"
MONITOR="$REPO_DIR/scripts/monitor_calibration_guardrails.py"
WATCHDOG_LOG="$STATE_DIR/watchdog.log"
RESTART_DELAY=10

# Exit codes from the monitor that mean "stop watching", not "crash":
#   0   - monitoring window completed cleanly
#   130 - interrupted (Ctrl-C / SIGINT)
DONE_EXIT_CODES=(0 130)

mkdir -p "$STATE_DIR"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "[$(ts)] WATCHDOG $*"; }

is_done_code() {
    local code=$1
    for c in "${DONE_EXIT_CODES[@]}"; do
        [ "$code" -eq "$c" ] && return 0
    done
    return 1
}

# Translate common signal/exit codes into a human-readable reason.
exit_reason() {
    case "$1" in
        0)   echo "clean exit (monitoring window complete)" ;;
        1)   echo "monitor reported reject/error (see alerts JSONL)" ;;
        130) echo "interrupted (SIGINT)" ;;
        137) echo "killed (SIGKILL / OOM?)" ;;
        143) echo "terminated (SIGTERM)" ;;
        *)   echo "unexpected exit code $1" ;;
    esac
}

log "starting watchdog for $MONITOR (pid=$$)"

while true; do
    python3 "$MONITOR" --duration-hours 168 --interval 60 --summary-interval 3600
    rc=$?
    reason="$(exit_reason "$rc")"

    if is_done_code "$rc"; then
        log "monitor exited rc=$rc — $reason; watchdog stopping."
        exit 0
    fi

    log "ALERT monitor exited rc=$rc — $reason; restarting in ${RESTART_DELAY}s"
    sleep "$RESTART_DELAY"
done
