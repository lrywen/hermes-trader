#!/usr/bin/env bash
# Two-tier full-suite gate (2026-10-23, R7 follow-up).
#
# The serial suite takes >25 min, so it cannot serve as an acceptance gate.
# This runner makes the suite gatable again:
#
#   tier 1  pytest -n N --dist loadfile   — bulk run, ~5-10 min on 12 cores
#   tier 2  every FAILED id is re-run standalone in a fresh process
#
# Verdict comes from tier 2 ONLY. Under 6-way parallel load a handful of
# tests flake on wall-clock margins or module-level state left by an
# unrelated file earlier in the same worker (failure sets vary run to run:
# observed 18/5/24 across three identical runs); every one of them passes
# standalone. Tier 2 separates that flake debt from real regressions.
#
# Usage:  bash scripts/run_full_tests.sh [N_WORKERS]     (default 6)
set -u
cd "$(dirname "$0")/.."

N="${1:-6}"
LOG="$(mktemp /tmp/full_suite_tier1.XXXXXX.log)"
trap 'rm -f "$LOG"' EXIT

echo "== tier 1: pytest -n ${N} --dist loadfile =="
python3 -m pytest tests/ -n "$N" --dist loadfile -q --tb=no -rf \
    -p no:cacheprovider 2>&1 | tee "$LOG" | tail -3

mapfile -t FAILED < <(grep '^FAILED ' "$LOG" | sed 's/^FAILED //; s/ - .*//' | sort -u)
if [ "${#FAILED[@]}" -eq 0 ]; then
    echo "== tier 2: nothing to re-run — SUITE GREEN =="
    exit 0
fi

echo "== tier 2: re-running ${#FAILED[@]} failure(s) standalone =="
REAL=()
for t in "${FAILED[@]}"; do
    if python3 -m pytest "$t" -q --tb=no --no-header -p no:cacheprovider >/dev/null 2>&1; then
        echo "  flake (passes standalone): $t"
    else
        echo "  REAL FAILURE:              $t"
        REAL+=("$t")
    fi
done

if [ "${#REAL[@]}" -eq 0 ]; then
    echo "== SUITE GREEN (all tier-1 failures were parallel-load flakes) =="
    exit 0
fi
echo "== SUITE RED: ${#REAL[@]} real failure(s) =="
exit 1
