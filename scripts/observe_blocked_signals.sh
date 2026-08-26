#!/usr/bin/env bash
# observe_blocked_signals.sh — 24-hour live observation of NEW-rule vetoes.
#
# Tails the hermes-trader container's session-log.jsonl and counts every
# ta_skip with signal=REJECTED (P0 RSI/extension veto) plus execute events
# blocked by risk gates (chop / counter-trend / crowded). Writes a rolling
# summary to /tmp/hermes_blocked_observations.jsonl every 5 minutes.
#
# Usage:
#   nohup bash /home/ldy/hermes-trader/scripts/observe_blocked_signals.sh \
#       > /tmp/hermes_observe.log 2>&1 &
#
# Stop: kill the PID printed at startup.

set -euo pipefail

OBSERVE_DURATION="${1:-86400}"   # default 24h in seconds
SUMMARY_FILE="/tmp/hermes_blocked_observations.jsonl"
RAW_LOG="/tmp/hermes_blocked_raw.jsonl"
START_TS=$(date +%s)
END_TS=$((START_TS + OBSERVE_DURATION))

echo "[$(date -Iseconds)] Observation started. PID=$$  duration=${OBSERVE_DURATION}s"
echo "[$(date -Iseconds)] Summary -> $SUMMARY_FILE"
echo "[$(date -Iseconds)] Raw log -> $RAW_LOG"

# Track the position we've read up to so we only tail NEW events.
LOG_OFFSET_FILE="/tmp/hermes_observe_offset.txt"

# Snapshot of existing log size so we only count events from now on.
docker exec hermes-trader sh -c 'wc -c < /data/session-log.jsonl' > "$LOG_OFFSET_FILE" 2>/dev/null || echo 0 > "$LOG_OFFSET_FILE"

write_summary() {
    local now
    now=$(date +%s)
    local elapsed=$((now - START_TS))

    # Count REJECTED vetoes by reason category
    local late_long=0 late_short=0 overext_long=0 overext_short=0
    local chop_block=0 crowded_block=0 counter_trend_block=0 confidence_block=0
    local other_block=0 total_rejected=0 total_blocked=0

    if [ -f "$RAW_LOG" ]; then
        late_long=$(grep -c 'late long' "$RAW_LOG" 2>/dev/null || echo 0)
        late_short=$(grep -c 'late short' "$RAW_LOG" 2>/dev/null || echo 0)
        overext_long=$(grep -c 'overextended long' "$RAW_LOG" 2>/dev/null || echo 0)
        overext_short=$(grep -c 'overextended short' "$RAW_LOG" 2>/dev/null || echo 0)
        chop_block=$(grep -c 'chop_blocked\|chop regime' "$RAW_LOG" 2>/dev/null || echo 0)
        crowded_block=$(grep -c 'CROWDED\|squeeze risk' "$RAW_LOG" 2>/dev/null || echo 0)
        counter_trend_block=$(grep -c 'counter.trend\|counter_trend' "$RAW_LOG" 2>/dev/null || echo 0)
        confidence_block=$(grep -c 'confidence.*<' "$RAW_LOG" 2>/dev/null || echo 0)
        total_rejected=$(grep -c '"signal": "REJECTED"' "$RAW_LOG" 2>/dev/null || echo 0)
        total_blocked=$(grep -c '"executed": false' "$RAW_LOG" 2>/dev/null || echo 0)
    fi

    # Unique coins vetoed
    local coins_vetoed=""
    if [ -f "$RAW_LOG" ]; then
        coins_vetoed=$(grep -oP '"coin":\s*"\K[^"]+' "$RAW_LOG" 2>/dev/null | sort -u | tr '\n' ',' | sed 's/,$//')
    fi

    TS=$(date +%s) \
    ELAPSED="$elapsed" \
    TOTAL_REJ="$total_rejected" \
    TOTAL_BLK="$total_blocked" \
    LATE_L="$late_long" \
    LATE_S="$late_short" \
    OVEREXT_L="$overext_long" \
    OVEREXT_S="$overext_short" \
    CHOP_BLK="$chop_block" \
    CROWDED_BLK="$crowded_block" \
    CTRTREND_BLK="$counter_trend_block" \
    CONF_BLK="$confidence_block" \
    COINS_VETOED="$coins_vetoed" \
    python3 - <<'PYEOF' >> "$SUMMARY_FILE"
import json, os
def _int(k, d=0):
    try: return int(os.environ.get(k, d))
    except ValueError: return d
elapsed = _int("ELAPSED")
summary = {
    "ts": _int("TS") * 1000,
    "event": "observation_summary",
    "elapsed_seconds": elapsed,
    "elapsed_hours": round(elapsed / 3600, 1),
    "total_rejected_by_ta_filter": _int("TOTAL_REJ"),
    "total_blocked_by_risk_gate": _int("TOTAL_BLK"),
    "vetoes": {
        "late_long_rsi_over75": _int("LATE_L"),
        "late_short_rsi_under25": _int("LATE_S"),
        "overextended_long": _int("OVEREXT_L"),
        "overextended_short": _int("OVEREXT_S"),
        "chop_regime": _int("CHOP_BLK"),
        "crowded_squeeze_risk": _int("CROWDED_BLK"),
        "counter_trend": _int("CTRTREND_BLK"),
        "low_confidence": _int("CONF_BLK"),
    },
    "coins_vetoed": os.environ.get("COINS_VETOED", ""),
}
print(json.dumps(summary))
PYEOF

    echo "[$(date -Iseconds)] Summary: REJECTED=$total_rejected BLOCKED=$total_blocked " \
         "late_long=$late_long late_short=$late_short overext_L=$overext_long overext_S=$overext_short " \
         "chop=$chop_block crowded=$crowded_block"
}

# Poll every 60 seconds for new events, write summary every 5 minutes.
POLL_INTERVAL=60
SUMMARY_INTERVAL=300
last_summary=$START_TS

while [ "$(date +%s)" -lt "$END_TS" ]; do
    offset=$(cat "$LOG_OFFSET_FILE")
    # Extract new bytes from container log since last offset
    docker exec hermes-trader sh -c "dd if=/data/session-log.jsonl bs=1 skip=$offset 2>/dev/null" >> "$RAW_LOG" 2>/dev/null || true
    # Update offset
    docker exec hermes-trader sh -c 'wc -c < /data/session-log.jsonl' > "$LOG_OFFSET_FILE" 2>/dev/null || true

    now=$(date +%s)
    if [ $((now - last_summary)) -ge "$SUMMARY_INTERVAL" ]; then
        write_summary
        last_summary=$now
    fi

    sleep "$POLL_INTERVAL"
done

# Final summary
write_summary
echo "[$(date -Iseconds)] Observation complete after $OBSERVE_DURATION seconds."
