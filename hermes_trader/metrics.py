"""Prometheus metrics for the trading agent.

The `/metrics` endpoint (served by `server.py`) is scraped by Prometheus. It is
deliberately **network-free**: every gauge is refreshed from local state only
(`memory`, the agent config, and the cross-process positions snapshot the loop
writes each cycle), so a scrape never hits Hyperliquid and never contends with
the loop's rate limiter. Process/GC collectors are auto-registered by
prometheus_client on import (they populate on Linux — i.e. in the container/k8s,
which is where the ops signal matters).
"""

from __future__ import annotations

import logging
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

EQUITY = Gauge("hermes_equity_usd", "Last known account equity in USD")
OPEN_POSITIONS = Gauge(
    "hermes_open_positions", "Open positions (from the loop snapshot)"
)
OPEN_NOTIONAL = Gauge(
    "hermes_open_notional_usd", "Sum of open position notional in USD"
)
UNREALIZED_PNL = Gauge(
    "hermes_unrealized_pnl_usd", "Sum of unrealized PnL across open positions in USD"
)
TRADES_TOTAL = Gauge("hermes_trades_total", "Number of recorded trades")
LIVE_MODE = Gauge("hermes_live_mode", "1 when agent mode is LIVE, 0 otherwise")

# ── Sizing/risk-overhaul deviation gauges (2026-08-26) ───────────────────
# These are set directly on the trade hot path (not via _refresh, which is
# scrape-driven) so the value reflects the most recent trade. Alert when:
#   SIZING_DSL_DEVIATION  > 5   (sizing-vs-DSL stop drift guard)
#   ACTUAL_STOP_DEVIATION > 10  (realized stop loss vs configured cap)
SIZING_DSL_DEVIATION = Gauge(
    "hermes_sizing_dsl_stop_deviation_pct",
    "Percent deviation between sizing-computed core stop and DSL-registered "
    "effective stop on the most recent entry (>5% = drift alarm)",
)
ACTUAL_STOP_DEVIATION = Gauge(
    "hermes_actual_stop_loss_deviation_pct",
    "Percent by which the most recent realized stop loss exceeded its "
    "configured cap (>10% = stop-overrun/slippage alarm)",
)

# ── DSL (Dynamic Stop-Loss) engine counters ────────────────────────────
# Incremented on the trade hot path by agents/dsl_exit.py so the /metrics
# scrape stays network-free. Alert when SAVE_ERRORS is non-zero over a
# 5m window — it means the floor/peak registry can't persist and a restart
# would lose ratchet state.
DSL_STATE_SAVE_ERRORS = Counter(
    "hermes_dsl_state_save_errors_total",
    "Number of failed attempts to persist the DSL state file after all "
    "retries are exhausted (one increment per _save_state() that gives up).",
)
DSL_EXITS = Counter(
    "hermes_dsl_exits_total",
    "DSL exit verdicts emitted by check(), labelled by reason "
    "(max_loss/floor_breach/hard_timeout/stale_flat_timeout).",
    ["reason"],
)
DSL_FLOOR_MOVES = Counter(
    "hermes_dsl_floor_moves_total",
    "Number of times a DSL floor actually moved (monotonic ratchet step).",
)
DSL_POSITIONS = Gauge(
    "hermes_dsl_positions",
    "Number of trackers currently held in the DSL registry.",
)
# Audit 2026-09-06 (E5, P2): state-file corruption was previously swallowed
# with a warning log and the registry silently reset to empty on restart.
# Any increment is page-worthy — the corrupt file was quarantined and the
# process fell back to .bak (or, failing that, an empty registry).
DSL_STATE_CORRUPT_ISOLATIONS = Counter(
    "hermes_dsl_state_corrupt_isolations_total",
    "Number of times the DSL state file failed to parse/migrate on load: "
    "the corrupt file was quarantined (.corrupt-<ts>) and .bak fallback "
    "was attempted in the same load call.",
)

# ── Executor decision counters ─────────────────────────────────────────
# Incremented on the executor hot path so a blocked/executed ratio and the
# distribution of block reasons are observable without scraping log lines.
# Labels are bounded enums (no coin / no free-text reason) to keep
# cardinality flat.
EXECUTOR_DECISIONS = Counter(
    "hermes_executor_decisions_total",
    "maybe_execute() outcomes, labelled by outcome "
    "(executed/blocked/shadow/mode_off/error).",
    ["outcome"],
)
EXECUTOR_ENTRIES = Counter(
    "hermes_executor_entries_total",
    "Live entries actually sent to the exchange, labelled by side (long/short).",
    ["side"],
)
EXECUTOR_SIZING_CLAMPED = Counter(
    "hermes_executor_sizing_clamped_total",
    "Sizing results that were clamped by a cap, labelled by clamp type "
    "(max_notional/leverage/gray_pct/min_notional/other).",
    ["clamp"],
)
# P3 (2026-09-05): business-level liveness signal. Incremented by
# notify_dispatch on an execute event whose ``executed`` flag is True
# (an actual fill delivered to the exchange), labelled by side. A flat 6h
# window with hermes_live_mode==1 and zero increase is the "no trades"
# silent-failure alert (HermesNoFill in k8s/prometheusrule.yaml). In
# SHADOW/mode_off no fills occur, so the alert gates on live mode to
# avoid false positives.
FILLS_TOTAL = Counter(
    "hermes_position_fills_total",
    "Actual position fills delivered to the exchange, labelled by side "
    "(long/short). Incremented by notify_dispatch on execute.executed=True.",
    ["side"],
)

# ── Backup-SL safety net (audit 2026-09-11, Q6/Q8) ─────────────────────
# A position whose server-side stop failed twice is queued in
# executor._pending_sl_retries for aggressive retry. The queue used to be
# purely in-memory, so a process restart dropped it and the position was left
# with NO exchange-side stop for the rest of its life (DSL soft stop being a
# common-cause dependent fallback). These metrics make that tail risk visible
# and the queue itself is now persisted to disk and reloaded on startup.
PENDING_SL_RETRIES = Gauge(
    "hermes_pending_sl_retries",
    "Number of positions currently awaiting a server-side stop re-arm "
    "(naked except for the DSL soft-stop loop). Any positive value is "
    "page-worthy.",
)
PENDING_SL_MISSING_TOTAL = Counter(
    "hermes_pending_sl_missing_total",
    "Number of times a position entered the pending-SL queue because its "
    "stop placement failed twice (one per newly-naked position).",
)
PENDING_SL_REARM_FAILURES = Counter(
    "hermes_pending_sl_rearm_failures_total",
    "Number of deferred stop re-arm attempts that still failed (excluding "
    "ambiguous openOrders lookups and in-backoff skips).",
)

# Audit 2026-09-11 (Q9): a bounded counter for best-effort NON-metric code
# branches that deliberately swallow an exception (config parse, regime
# detection, audit fork) so a silent failure is at least countable instead of
# buried in log noise. Pure Prometheus metric guards intentionally do NOT use
# this (instrumenting an instrumentation failure adds no signal).
SWALLOWED_ERRORS = Counter(
    "hermes_swallowed_errors_total",
    "Best-effort non-metric branch swallowed an exception, labelled by a "
    "bounded call-site name.",
    ["func"],
)

# ── P3-1: full-chain instrumentation (2026-08-27) ──────────────────────
# Every hot-path metric follows the existing contract:
#   * imported lazily inside the calling function and wrapped in
#     ``try/except Exception: pass`` so a metrics failure never breaks a
#     trade/LLM call;
#   * labels are BOUNDED ENUMS — never a coin name or free-text reason
#     (unknown values normalise to ``other``);
#   * gauges are either set directly on the hot path or refreshed in
#     ``_refresh()`` from local, network-free state.
# Histogram buckets are per-domain (LLM up to ~60s timeouts; debate up to
# the ~24s cap; disk flushes sub-second).

# ── LLM gateway (research.py) ──────────────────────────────────────────
# Alert when:
#   LLM_REQUESTS outcome=error/empty rate > 20% over 15m
#   LLM_REQUEST_DURATION p95 > 45s (approaching the 60s call timeout)
#   LLM_CIRCUIT_TRIPS > 0 (upstream dead — every coin is degrading)
LLM_REQUEST_DURATION = Histogram(
    "hermes_llm_request_duration_seconds",
    "Wall duration of one OpenRouter/OpenAI-compatible request, labelled "
    "by caller path (call_ai/debate_direct) and terminal outcome "
    "(ok/empty/error/circuit_open/no_key).",
    ["path", "outcome"],
    buckets=(0.5, 1.0, 2.5, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0),
)
LLM_REQUESTS = Counter(
    "hermes_llm_requests_total",
    "LLM requests reaching the gateway, labelled by caller path and terminal "
    "outcome (ok/empty/error/circuit_open/no_key).",
    ["path", "outcome"],
)
LLM_RETRIES = Counter(
    "hermes_llm_retries_total",
    "Rate-limit/availability retries inside one request, labelled by cause "
    "(rate_limit/network/continuation).",
    ["cause"],
)
LLM_CIRCUIT_TRIPS = Counter(
    "hermes_llm_circuit_trips_total",
    "Number of times the research-path LLM circuit breaker opened after the "
    "consecutive-failure threshold.",
)
LLM_CIRCUIT_STATE = Gauge(
    "hermes_llm_circuit_state",
    "1 while the research-path LLM circuit breaker is OPEN (calls "
    "short-circuit), 0 when closed. Set on every gateway entry.",
)

# ── Native in-process debate (research.py) ─────────────────────────────
DEBATE_STAGE_DURATION = Histogram(
    "hermes_debate_stage_duration_seconds",
    "Wall duration of one debate stage, labelled by stage (bull/bear/synth/"
    "bull_bear/total) and outcome (ok/failed/empty).",
    ["stage", "outcome"],
    buckets=(0.5, 1.0, 2.5, 5.0, 8.0, 12.0, 18.0, 25.0),
)
DEBATE_FALLBACKS = Counter(
    "hermes_debate_fallbacks_total",
    "Debate attempts that gave up and fell back to the single-LLM path, "
    "labelled by bounded reason (bull_bear_failed/synth_failed/"
    "synth_empty/other).",
    ["reason"],
)
DEBATE_CACHE_LOOKUPS = Counter(
    "hermes_debate_cache_lookups_total",
    "Debate verdict cache lookups, labelled by result (hit/stale/miss).",
    ["result"],
)
DEBATE_CACHE_ENTRIES = Gauge(
    "hermes_debate_cache_entries",
    "Current number of entries in the in-process debate verdict cache "
    "(capacity-bounded; set after each write/sweep).",
)
DEBATE_CACHE_EVICTIONS = Counter(
    "hermes_debate_cache_evictions_total",
    "Debate cache entries removed, labelled by reason (expired/capacity).",
    ["reason"],
)

# ── Risk gates (risk_gates.py) ─────────────────────────────────────────
# Alert when RISK_GATE_BLOCKS for a single gate fires repeatedly (a gate
# may be mis-calibrated or the market regime shifted).
RISK_GATE_BLOCKS = Counter(
    "hermes_risk_gate_blocks_total",
    "Trade proposals blocked per gate, labelled by the fixed gate key "
    "(confidence/max_concurrent/notional_cap/daily_loss/daily_giveback/"
    "liquidity/short_liquidity/coin_filter/cooldown/coin_circuit/"
    "global_halt/opposite_guard/correlation/equity_risk/market_regime/"
    "news/debate/ta_late_entry/other).",
    ["gate"],
)
RISK_GATE_REGIME_VERDICTS = Counter(
    "hermes_risk_gate_regime_verdicts_total",
    "market_regime_gate verdicts, labelled by the bounded via code "
    "(aligned/neutral/chop_conviction/chop_blocked/confidence/composite/"
    "crowded_squeeze/blocked/blocked_bypass/trigger/other).",
    ["via"],
)
# ta_late_entry_gate (deep audit 高危项, 2026-08-30): the gate re-fetches 4h
# (+15m) candles and recomputes RSI/ADX/ATR immediately before order
# placement. Alert when the p95 of the whole evaluation approaches 10ms
# (slippage budget). Bounded labels: gate is fixed; outcome ∈
# ok/shadow_block/enforce_block/pass/data_missing/error/disabled.
RISK_GATE_DURATION = Histogram(
    "hermes_risk_gate_duration_seconds",
    "Wall duration of one risk-gate evaluation, labelled by gate and "
    "bounded outcome. Buckets centre on the 10ms late-entry recompute "
    "budget (0.005–0.05s).",
    ["gate", "outcome"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
# Late-entry gate verdicts. SHADOW/LIVE PARITY (2026-09-04): the gate is
# mode-independent once active and ALWAYS enforces, so the gray-release
# would_block verdict no longer exists — a veto blocks in both modes. Labels:
# mode (off/enforce; "shadow" legacy value is normalised to enforce), side
# (long/short), verdict (pass/block/data_missing).
TA_LATE_ENTRY_VERDICTS = Counter(
    "hermes_ta_late_entry_verdicts_total",
    "ta_late_entry_gate verdicts, labelled by mode, side and bounded "
    "verdict (pass/block/data_missing).",
    ["mode", "side", "verdict"],
)
# Candle cache outcome for fetch_hl_candles (deep audit ta_late_entry R7 /
# Phase 0, 2026-08-30). result ∈ hit (served from TTL cache), coalesced
# (another thread's in-flight fetch was awaited), miss (this call issued the
# HTTP). Cache-miss rate per interval quantifies how often the gate/screen
# actually pay a cold weight-20 candleSnapshot call vs reuse the 90s cache.
CANDLE_CACHE_LOOKUPS = Counter(
    "hermes_candle_cache_lookups_total",
    "fetch_hl_candles outcomes, labelled by candle interval and result "
    "(hit/coalesced/miss). Miss rate exposes cold-HTTP frequency per timeframe.",
    ["interval", "result"],
)
# Audit 2026-09-06 (C9): the fetch-side quality gate (_fetch_hl_candles_raw)
# previously only logged when a candleSnapshot was gappy/stale/truncated — a
# sustained 429-storm feed degradation was invisible to alerts until a trade
# was silently fail-closed. One increment per issue found on each cold HTTP
# fetch (cache hits are not re-assessed). Labels are bounded: interval is a
# fixed timeframe enum; issue ∈ gaps/stale/low_coverage/thin/other.
CANDLE_QUALITY_ISSUES = Counter(
    "hermes_candle_quality_issues_total",
    "Candle quality-gate issues found on cold candleSnapshot fetches "
    "(fail-closed: the bad series is returned but never cached), labelled by "
    "interval and bounded issue (gaps/stale/low_coverage/thin/other).",
    ["interval", "issue"],
)
# Audit 2026-09-06 (C9): per-bar parse rejections (malformed payload, NaN/Inf
# OHLC, out-of-order timestamps). A rising rate means the upstream wire format
# or feed is degraded. cause ∈ malformed/non_finite/out_of_order.
CANDLE_PARSE_DROPPED = Counter(
    "hermes_candle_parse_dropped_total",
    "Raw candle bars dropped while parsing a candleSnapshot response, "
    "labelled by interval and bounded cause "
    "(malformed/non_finite/out_of_order).",
    ["interval", "cause"],
)
# Audit 2026-09-06 (C9): freshness gauge — age of the newest CLOSED bar on the
# most recent cold fetch per interval. Alert when this approaches 2x the bar
# duration (the gate's stale threshold), i.e. the feed is lagging before any
# gate trip. Set on every cold fetch (0 when no closed bar was usable).
CANDLE_CLOSED_BAR_AGE = Gauge(
    "hermes_candle_closed_bar_age_seconds",
    "Age in seconds of the newest closed candle bar on the most recent cold "
    "candleSnapshot fetch, labelled by interval (feed-lag early warning).",
    ["interval"],
)

# ── Trade-side tiered circuit breakers (executor.py / memory.py) ───────
TRADE_CIRCUIT_TRIPS = Counter(
    "hermes_trade_circuit_trips_total",
    "Trade-side circuit breakers armed at the close chokepoint, labelled by "
    "scope (coin/global).",
    ["scope"],
)
TRADE_CIRCUIT_STATE = Gauge(
    "hermes_trade_circuit_state",
    "1 while the given scope of breaker is armed, 0 when clear. Refreshed "
    "network-free from local memory state. Scope: global/coin_armed.",
    ["scope"],
)
# ── Market-level tail-risk circuit breaker (market_circuit.py) ─────────
# roadmap §3 (2026-09-04). One verdict per loop tick while mode != off.
# Bounded labels: mode (shadow/enforce), verdict (trip/no_trip/data_missing).
# In shadow mode a `trip` is a would-trigger (halt NOT armed); compare against
# the subsequent tape before flipping mode=enforce.
MARKET_CIRCUIT_VERDICTS = Counter(
    "hermes_market_circuit_verdicts_total",
    "market_circuit evaluations, labelled by mode and bounded verdict "
    "(trip/no_trip/data_missing). In shadow mode trip = would-trigger.",
    ["mode", "verdict"],
)
# CS-F (2026-09-08): the Counter above is incremented in the trading-loop
# process while Prometheus scrapes the web process; without multi-process mode
# those increments never reach the scraped registry, so the market_circuit
# alerts were blind. The loop instead rewrites a whole-file heartbeat state
# (agents/market_circuit_state.py) on the shared /data volume; these gauges
# are refreshed from that file in _refresh() so BOTH processes are visible.
# Sentinels (a plain Gauge always exports a 0.0 sample even when never set,
# so absent_over_time() cannot distinguish "never wrote"; use explicit values):
#   no heartbeat file yet  → last_eval_ts = 0 (HeartbeatAbsent fires on == 0);
#   file deleted after run → last_eval_ts = 0 likewise;
#   stale loop             → age keeps growing against the last real ts.
MARKET_CIRCUIT_LAST_EVAL_TS = Gauge(
    "hermes_market_circuit_last_eval_timestamp_seconds",
    "Unix timestamp of the most recent market_circuit evaluation "
    "(cross-process heartbeat written by the trading loop); 0 = no heartbeat "
    "file yet (loop never evaluated or state file missing).",
)
MARKET_CIRCUIT_EVAL_AGE = Gauge(
    "hermes_market_circuit_eval_age_seconds",
    "Seconds since the most recent market_circuit evaluation. Grows without "
    "bound while the loop is not evaluating (crashed/off/stuck); 0 when no "
    "heartbeat has ever been written (use last_eval_timestamp == 0 for that).",
)
MARKET_CIRCUIT_STATE = Gauge(
    "hermes_market_circuit_state",
    "Latest market_circuit verdict state, labelled by mode: 0=clear, "
    "1=tripped (halt armed or shadow would-trip), 2=data_missing, 3=off, "
    "4=error/unknown (also exported for mode=unknown while no heartbeat).",
    ["mode"],
)
MARKET_CIRCUIT_VERDICTS_CUMULATIVE = Gauge(
    "hermes_market_circuit_verdicts_cumulative",
    "Cumulative market_circuit evaluations persisted in the heartbeat state "
    "file (survives loop restart), labelled by mode and bounded verdict; "
    "cross-process replacement for the process-local verdicts Counter. In "
    "shadow mode trip = would-trigger.",
    ["mode", "verdict"],
)

# ── Gray-release decay / regime factors (roadmap §1/§2, 2026-09-04) ────
# One observation per evaluation while mode != off. Bounded labels:
# mode (shadow/enforce), outcome (applied/no_change/would_block). A
# `would_block` outcome means the raw value passed the gate but the
# decayed/calibrated one would not — shadow-only counterfactual.
CONFIDENCE_DECAY_OBSERVATIONS = Counter(
    "hermes_confidence_decay_observations_total",
    "AI-confidence freshness-decay evaluations (executor.py), labelled by "
    "mode and bounded outcome (applied/no_change/would_block).",
    ["mode", "outcome"],
)
SIGNAL_AGE_DECAY_OBSERVATIONS = Counter(
    "hermes_signal_age_decay_observations_total",
    "Perception setup-age decay evaluations (perception.py), labelled by "
    "mode and bounded outcome (applied/no_change/would_block).",
    ["mode", "outcome"],
)
ATR_REGIME_CALIB_OBSERVATIONS = Counter(
    "hermes_atr_regime_calib_observations_total",
    "ATR volatility-regime stop-width factor evaluations (sizing.py via "
    "executor.py), labelled by mode and bounded outcome "
    "(applied/no_change).",
    ["mode", "outcome"],
)
# Audit 2026-09-06 (F2, engineering hygiene): every shadow/audit JSONL goes
# through shadow_log.append_jsonl(); previously the 8 per-feature writers each
# swallowed OSError with only a warning log, so a persistently unwritable
# ~/.hermes-trading silently lost audit data with no metric. One increment per
# append (or its rotation) that fails. Labelled by stream (logical shadow name).
SHADOW_LOG_WRITE_ERRORS = Counter(
    "hermes_shadow_log_write_errors_total",
    "Shadow/audit JSONL append or rotation attempts that failed after being "
    "swallowed (best-effort audit logs must never break the trade path).",
    ["stream"],
)
# Audit 2026-09-06 (C1): the five memory-backed circuit gates (coin_circuit,
# global_halt, consecutive_loss, per_coin_daily_loss, drawdown) deliberately
# fail-OPEN when their state read raises — a transient memory/state read fault
# must not become an account-wide trading DoS (locked by
# test_audit_bf2_bf6_bf7 + the gate docstring contract). That fail-open was
# previously silent (logger.debug), so a persistently broken memory read that
# silently disabled the kill-switch was invisible. We keep pass:True but emit
# an error log + this counter so ops can alert on a breaker that is blind.
MEMORY_GATE_READ_ERRORS = Counter(
    "hermes_memory_gate_read_errors_total",
    "Times a memory-backed circuit breaker's state read raised and the gate "
    "fell back to fail-open (pass=True). The breaker is BLIND while this "
    "increments — alert on any sustained rate.",
    ["gate"],
)

# ── State persistence (dsl_exit.py / memory.py) ────────────────────────
# Alert when save/flush p95 > 0.5s (disk contention) or FLUSH_ERRORS /
# save outcome=failed increments (a sick disk risks lost state).
DSL_STATE_SAVE_DURATION = Histogram(
    "hermes_dsl_state_save_duration_seconds",
    "Wall duration of one DSL registry _save_state() (lock + retries), "
    "labelled by outcome (ok/failed).",
    ["outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
DSL_STATE_DIRTY = Gauge(
    "hermes_dsl_state_dirty",
    "1 when the DSL registry has unsaved dirty state pending the next "
    "_save_state() retry, 0 when clean.",
)
MEMORY_FLUSH_DURATION = Histogram(
    "hermes_memory_flush_duration_seconds",
    "Wall duration of one agent-memory flush() that took the write path, "
    "labelled by force (true/false) and outcome (ok/failed).",
    ["force", "outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
MEMORY_FLUSH_ERRORS = Counter(
    "hermes_memory_flush_errors_total",
    "Agent-memory flush() write attempts that raised after retries "
    "(previously uncounted — the dirty flag retries but nothing surfaced "
    "a persistently failing disk).",
)
# Audit 2026-09-06 (E5, P2): same corruption-isolation story as the DSL
# state file, for .agent-memory.json. The append-only events.jsonl replay
# still runs afterwards, so an increment means memory was rebuilt/recovered
# rather than silently lost.
MEMORY_CORRUPT_ISOLATIONS = Counter(
    "hermes_memory_corrupt_isolations_total",
    "Number of times .agent-memory.json failed to parse on load: the "
    "corrupt file was quarantined (.corrupt-<ts>), .bak fallback was "
    "attempted, and the events.jsonl replay rebuild ran on top.",
)
NOTIFY_DISPATCH_ERRORS = Counter(
    "hermes_notify_dispatch_errors_total",
    "notify_dispatch.dispatch() card-render/send attempts that raised and "
    "were swallowed (previously logged at debug and invisible — a malformed "
    "record, e.g. None daily_pnl on a killswitch, silently dropped the alert).",
)
# ── R11-A1: send-side resilience (retry + circuit breaker + fallback) ────
# Alert when NOTIFY_SEND_FAILURES is incrementing for 5m+ — a webhook is
# degraded. NOTIFY_CIRCUIT_OPEN > 0 means a channel has been auto-quarantined.
NOTIFY_SEND_RETRIES = Counter(
    "hermes_notify_send_retries_total",
    "Feishu send attempts that backed off and retried after a transient "
    "(429 / 5xx / network) error. Labelled by channel URL.",
    ["channel"],
)
NOTIFY_SEND_FAILURES = Counter(
    "hermes_notify_send_failures_total",
    "Feishu send attempts that exhausted retries against a channel "
    "(and triggered the per-channel circuit breaker).",
    ["channel"],
)
NOTIFY_CIRCUIT_OPEN = Gauge(
    "hermes_notify_circuit_open",
    "1 while the per-channel circuit breaker is open and sends are being "
    "short-circuited, 0 when closed. Set on every send attempt.",
    ["channel"],
)
NOTIFY_FALLBACK_USED = Counter(
    "hermes_notify_fallback_used_total",
    "Times a card was delivered via a non-primary channel because the "
    "primary channel returned a non-2xx after exhausting retries.",
    ["category"],
)

# R11-C1: per-endpoint serialization gate (rate_limit.py). A high wait
# means many concurrent workers are trying to enter the same endpoint
# and being serialized by the in-process gate; alert on this before
# the shared token bucket starts rejecting them.
HL_RATE_GATE_WAIT = Histogram(
    "hermes_hl_rate_gate_wait_seconds",
    "Time spent waiting for the per-endpoint serialization gate (R11-C1).",
    ["endpoint"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
# R11-F1: WS gauges — surface HyperliquidWebSocket.get_diag() for the
# PrometheusRule alerts in k8s/prometheusrule.yaml. The /metrics
# endpoint stays network-free (R11-D1's get_diag reads from a local
# in-process snapshot), so the scrape path never contends with the
# trading loop's rate limiter.
WS_LAST_SEQ = Gauge(
    "hermes_ws_last_seq",
    "Sequence number of the most recent accepted allMids frame (R11-D1).",
)
WS_DROPPED_DUP = Counter(
    "hermes_ws_dropped_dup_total",
    "allMids frames dropped because their seq duplicated the last accepted (R11-D1).",
)
WS_DROPPED_STALE = Counter(
    "hermes_ws_dropped_stale_total",
    "allMids frames dropped because their seq was less than the last accepted (R11-D1).",
)
WS_DATA_AGE_S = Gauge(
    "hermes_ws_data_age_seconds",
    "Age (in seconds) of the most recent applied allMids payload (R11-D1).",
)
WS_APP_HEARTBEAT_AGE_S = Gauge(
    "hermes_ws_app_heartbeat_age_seconds",
    "Age (in seconds) since the app-level heartbeat last fired (R11-D1).",
)
# Phase-4 P1: REST weight observability. Counters live in the flock'd shared
# token-bucket state file (/dev/shm) so the server /metrics process sees the
# trading loop's traffic too (HL's 1200 weight/min budget is per-IP, shared
# across both processes). Gauges are used (not Counters) because the value is
# read cross-process from a file; a scrape stays network-free. Alert when
# granted_weight's 1m rate approaches 800/min (WS degraded) — with WS healthy
# it should stay under ~300/min.
HL_REST_GRANTED_WEIGHT = Gauge(
    "hermes_hl_rest_granted_weight_total",
    "Cumulative request-weight granted by the HL token bucket since the "
    "state file was created (cross-process; Phase-4 P1).",
)
HL_REST_GRANTED_REQUESTS = Gauge(
    "hermes_hl_rest_granted_requests_total",
    "Cumulative number of REST requests granted by the HL token bucket "
    "(cross-process; Phase-4 P1).",
)
HL_REST_DENIED_REQUESTS = Gauge(
    "hermes_hl_rest_denied_requests_total",
    "Cumulative requests skipped because the HL rate budget was exhausted "
    "after max_wait (cross-process; Phase-4 P1).",
)
HL_REST_PENALIZED_REQUESTS = Gauge(
    "hermes_hl_rest_penalized_requests_total",
    "Cumulative requests that received a 429 and drained the bucket "
    "(cross-process; Phase-4 P1).",
)
HL_REST_TOKENS_AVAILABLE = Gauge(
    "hermes_hl_rest_tokens_available",
    "Token-bucket balance right now (0 = saturated, callers will queue; "
    "Phase-4 P1).",
)


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _refresh() -> None:
    """Pull current values from local state. Never raises — a partial scrape
    beats a 500 that blinds the dashboard."""
    try:
        from hermes_trader.agents.memory import memory

        memory.load()
        EQUITY.set(_to_float(memory.get_full_state().get("equity", 0)))
        TRADES_TOTAL.set(len(memory.get_all_trades() or []))
    except Exception as e:
        logger.debug(f"[metrics] memory read failed: {e}")

    try:
        from hermes_trader.agents.config_store import read_agent_config

        mode = str(read_agent_config().get("mode", "OFF")).upper()
        LIVE_MODE.set(1.0 if mode == "LIVE" else 0.0)
    except Exception as e:
        logger.debug(f"[metrics] config read failed: {e}")

    try:
        from hermes_trader.positions_snapshot import read_snapshot

        snap = read_snapshot(max_age_s=600.0) or {}
        count = 0
        notional = 0.0
        upnl = 0.0
        for entry in snap.get("asset_positions", []):
            pos = entry.get("position", {}) if isinstance(entry, dict) else {}
            if _to_float(pos.get("szi")) == 0:
                continue
            count += 1
            notional += abs(_to_float(pos.get("positionValue")))
            upnl += _to_float(pos.get("unrealizedPnl"))
        OPEN_POSITIONS.set(count)
        OPEN_NOTIONAL.set(notional)
        UNREALIZED_PNL.set(upnl)
    except Exception as e:
        logger.debug(f"[metrics] snapshot read failed: {e}")

    # P3-1: trade-side tiered breakers — read-only snapshot, no mutation
    # (circuit_snapshot never purges; a scrape must not alter trading state).
    try:
        from hermes_trader.agents.memory import memory

        cs = memory.circuit_snapshot()
        TRADE_CIRCUIT_STATE.labels(scope="global").set(1.0 if cs.get("global_halt") else 0.0)
        TRADE_CIRCUIT_STATE.labels(scope="coin_armed").set(float(cs.get("armed_coins", 0)))
    except Exception as e:
        logger.debug(f"[metrics] circuit read failed: {e}")

    # P3-1: debate cache size — local in-process dict, network-free.
    try:
        from hermes_trader.agents.research import _debate_cache

        DEBATE_CACHE_ENTRIES.set(float(len(_debate_cache)))
    except Exception as e:
        logger.debug(f"[metrics] debate cache read failed: {e}")

    # R11-F1: WS diag snapshot. The WebSocket singleton is held by
    # hl_client; if it isn't started (e.g. before trading_loop reaches
    # start_ws_mids), the gauges stay at zero, which is the correct
    # "no data yet" signal. The Counters (dropped_dup/dropped_stale)
    # are inc()'d directly by ws_client so they are already
    # up-to-date; we only refresh the Gauges here.
    try:
        from hermes_trader.client.hl_client import _ws_mids_instance

        ws = _ws_mids_instance
        if ws is not None and hasattr(ws, "get_diag"):
            diag = ws.get_diag()
            WS_LAST_SEQ.set(float(diag.get("last_seq", 0)))
            WS_DATA_AGE_S.set(float(diag.get("data_age_s", 0.0)))
            try:
                snap = ws.get_snapshot()
                WS_APP_HEARTBEAT_AGE_S.set(
                    max(0.0, time.time() - float(snap.app_heartbeat_at))
                )
            except Exception:
                # Snapshot can be momentarily racy; never let a gauge
                # read tear the metrics endpoint.
                pass
    except Exception as e:
        logger.debug(f"[metrics] ws diag read failed: {e}")

    # Phase-4 P1: HL REST rate-limiter counters. The shared bucket keeps its
    # cumulative totals in the flock'd /dev/shm state file, so this read is
    # network-free and reflects BOTH processes (trading loop + server). The
    # in-process fallback bucket reports its own counters with shared=False.
    try:
        from hermes_trader.client.rate_limit import HL_LIMITER

        if hasattr(HL_LIMITER, "stats"):
            st = HL_LIMITER.stats()
            HL_REST_GRANTED_WEIGHT.set(float(st.get("granted_weight", 0.0)))
            HL_REST_GRANTED_REQUESTS.set(float(st.get("granted_requests", 0)))
            HL_REST_DENIED_REQUESTS.set(float(st.get("denied_requests", 0)))
            HL_REST_PENALIZED_REQUESTS.set(float(st.get("penalized_requests", 0)))
            HL_REST_TOKENS_AVAILABLE.set(float(st.get("tokens_available", 0.0)))
    except Exception as e:
        logger.debug(f"[metrics] hl rate stats read failed: {e}")

    # CS-F: market_circuit cross-process heartbeat. The trading loop rewrites
    # the state file each tick. A missing/corrupt file (feature never ran /
    # file deleted / unreadable) is an EXPLICIT sentinel — last_eval_ts=0
    # (a plain Gauge always exports 0.0 even if never set, so absent() cannot
    # distinguish "never wrote") and state=4 for mode=unknown. Labelled gauges
    # are cleared each scrape so a mode change never leaves phantom samples.
    try:
        from hermes_trader.agents.market_circuit_state import (
            STATE_ERROR,
            read_state,
        )

        st = read_state()
        MARKET_CIRCUIT_STATE.clear()
        MARKET_CIRCUIT_VERDICTS_CUMULATIVE.clear()
        if st is None:
            MARKET_CIRCUIT_LAST_EVAL_TS.set(0.0)
            MARKET_CIRCUIT_EVAL_AGE.set(0.0)
            MARKET_CIRCUIT_STATE.labels(mode="unknown").set(float(STATE_ERROR))
        else:
            ts = _to_float(st.get("ts"))
            mode = str(st.get("mode", "off")) or "off"
            MARKET_CIRCUIT_LAST_EVAL_TS.set(ts if ts > 0 else 0.0)
            MARKET_CIRCUIT_EVAL_AGE.set(
                max(0.0, time.time() - ts) if ts > 0 else 0.0
            )
            try:
                state_val = float(int(st.get("state", STATE_ERROR)))
            except (TypeError, ValueError):
                state_val = float(STATE_ERROR)
            MARKET_CIRCUIT_STATE.labels(mode=mode).set(state_val)
            counts = st.get("counts")
            if isinstance(counts, dict):
                for m, bucket in counts.items():
                    if not isinstance(m, str) or not isinstance(bucket, dict):
                        continue
                    for verdict, n in bucket.items():
                        if isinstance(verdict, str):
                            MARKET_CIRCUIT_VERDICTS_CUMULATIVE.labels(
                                mode=m, verdict=verdict
                            ).set(_to_float(n))
    except Exception as e:
        logger.debug(f"[metrics] market_circuit state read failed: {e}")


def render_metrics() -> tuple[bytes, str]:
    """Refresh gauges and return (body, content_type) for the HTTP response."""
    _refresh()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
