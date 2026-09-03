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
# Shadow/enforce verdicts of the late-entry gate. In shadow mode (the
# gray-release default) would_block=1 verdicts are recorded but never stop
# the order; compare against post-entry price action for 3-7 days before
# flipping mode=enforce. Bounded labels: mode (off/shadow/enforce), side
# (long/short), verdict (pass/would_block/block/data_missing).
TA_LATE_ENTRY_VERDICTS = Counter(
    "hermes_ta_late_entry_verdicts_total",
    "ta_late_entry_gate verdicts, labelled by mode, side and bounded "
    "verdict (pass/would_block/block/data_missing).",
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


def render_metrics() -> tuple[bytes, str]:
    """Refresh gauges and return (body, content_type) for the HTTP response."""
    _refresh()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
