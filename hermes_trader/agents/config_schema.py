"""F27: single source of truth for agent-config key types, defaults and ranges.

The Pydantic model below declares every typed ``.agent-config.json`` key
exactly once: the annotation gives the type, ``Field(default=...)`` the
canonical default and ``Field(ge=/le=/gt=)`` the numeric range. Nested
dict values (``dsl_exit``, ``debate_gate`` …) are accepted as objects
without deep validation — their consumers use sparse ``.get`` access.

``validate_config_updates`` deliberately keeps the historical *strict*
isinstance acceptance matrix (bool is not accepted as int/float, strings
are not coerced) instead of Pydantic's lax coercion; range bounds are
reflected from the model field metadata so the bounds table no longer
has to be hand-maintained alongside the defaults.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Optional, get_origin

from pydantic import BaseModel, ConfigDict, Field

from hermes_trader.agents.config_store import CANONICAL_DEFAULTS


def _dict_default(key: str) -> dict[str, Any]:
    return deepcopy(CANONICAL_DEFAULTS[key])


def _list_default(key: str) -> list[Any]:
    return list(CANONICAL_DEFAULTS[key])


class _ConfigPatch(BaseModel):
    """Declarative schema for the typed agent-config keys.

    ``extra="allow"`` keeps unknown keys round-tripping through the model;
    the whitelist check in ``validate_config_updates`` decides whether they
    are an error (strict callers) or ignored (the legacy merge endpoint).
    ``_comment`` can't be a Pydantic field (underscore prefix) and is
    accepted as-is, matching historical behavior.
    """

    model_config = ConfigDict(extra="allow")

    # ── scalars: strings / bools ──────────────────────────────────────────
    mode: str = Field(default=CANONICAL_DEFAULTS["mode"])
    enable_crypto: bool = Field(default=CANONICAL_DEFAULTS["enable_crypto"])
    enable_hip3: bool = Field(default=CANONICAL_DEFAULTS["enable_hip3"])
    conviction_sizing: bool = Field(default=CANONICAL_DEFAULTS["conviction_sizing"])
    whale_regime_bypass: bool = Field(default=CANONICAL_DEFAULTS["whale_regime_bypass"])
    block_counter_trend_bypass: bool = Field(default=CANONICAL_DEFAULTS["block_counter_trend_bypass"])
    breakout_force_execute: bool = Field(default=CANONICAL_DEFAULTS["breakout_force_execute"])
    spread_gate_fail_open: bool = Field(default=CANONICAL_DEFAULTS["spread_gate_fail_open"])
    override_requires_ai: bool = Field(default=CANONICAL_DEFAULTS["override_requires_ai"])
    whale_scan_bypass: bool = Field(default=CANONICAL_DEFAULTS["whale_scan_bypass"])
    # F27: four bool keys previously missing from the hand-kept type table.
    composite_force_execute: bool = Field(default=CANONICAL_DEFAULTS["composite_force_execute"])
    ta_sidestep_force_execute: bool = Field(default=CANONICAL_DEFAULTS["ta_sidestep_force_execute"])
    whale_force_execute: bool = Field(default=CANONICAL_DEFAULTS["whale_force_execute"])
    trend_surface_enabled: bool = Field(default=CANONICAL_DEFAULTS["trend_surface_enabled"])
    # B-M11 (deep audit 2026-08-28): opt-in auto-flatten when the global halt
    # or per-coin circuit breaker trips (default off; open-blocking only).
    auto_flatten_on_global_halt: bool = Field(default=CANONICAL_DEFAULTS["auto_flatten_on_global_halt"])
    auto_flatten_on_coin_circuit: bool = Field(default=CANONICAL_DEFAULTS["auto_flatten_on_coin_circuit"])

    # ── scalars: ints ─────────────────────────────────────────────────────
    leverage: int = Field(default=CANONICAL_DEFAULTS["leverage"], ge=1, le=50)
    max_concurrent: int = Field(default=CANONICAL_DEFAULTS["max_concurrent"], ge=0)
    cooldown_min: int = Field(default=CANONICAL_DEFAULTS["cooldown_min"], ge=0, le=100_000)
    max_crypto_long_correlated: int = Field(default=CANONICAL_DEFAULTS["max_crypto_long_correlated"], ge=0, le=50)
    loss_cooldown_min: int = Field(default=CANONICAL_DEFAULTS["loss_cooldown_min"], ge=0, le=100_000)
    min_ai_close_hold_min: int = Field(default=CANONICAL_DEFAULTS["min_ai_close_hold_min"], ge=0, le=100_000)
    funding_lookback_hours: int = Field(default=CANONICAL_DEFAULTS["funding_lookback_hours"], ge=1, le=720)
    # R9/P2-3: news gate freshness window (days) and Brave headline cache TTL (s).
    news_freshness_days: int = Field(default=CANONICAL_DEFAULTS["news_freshness_days"], ge=1, le=365)
    news_cache_ttl_s: int = Field(default=CANONICAL_DEFAULTS["news_cache_ttl_s"], ge=0, le=86_400)
    research_cooldown_min: int = Field(default=CANONICAL_DEFAULTS["research_cooldown_min"], ge=0, le=100_000)
    held_research_interval_min: int = Field(default=CANONICAL_DEFAULTS["held_research_interval_min"], ge=0, le=100_000)
    force_execute_composite: int = Field(default=CANONICAL_DEFAULTS["force_execute_composite"], ge=0, le=100)
    ta_sidestep_min_slow_burn_count: int = Field(default=CANONICAL_DEFAULTS["ta_sidestep_min_slow_burn_count"], ge=0, le=100_000)
    force_execute_slow_burn_count: int = Field(default=CANONICAL_DEFAULTS["force_execute_slow_burn_count"], ge=0, le=100_000)

    # ── scalars: floats (ints accepted too, bools excluded) ───────────────
    equity_fraction_per_trade: float = Field(default=CANONICAL_DEFAULTS["equity_fraction_per_trade"], gt=0, le=1)
    min_ai_confidence: float = Field(default=CANONICAL_DEFAULTS["min_ai_confidence"], ge=0, le=1)
    max_trade_notional_usd: float = Field(default=CANONICAL_DEFAULTS["max_trade_notional_usd"], ge=0.0)
    max_total_notional_pct: float = Field(default=CANONICAL_DEFAULTS["max_total_notional_pct"], ge=0.0, le=50.0)
    max_daily_loss_usd: float = Field(default=CANONICAL_DEFAULTS["max_daily_loss_usd"], le=0.0)
    daily_giveback_halt_pct: float = Field(default=CANONICAL_DEFAULTS["daily_giveback_halt_pct"], ge=0.0, le=1.0)
    daily_giveback_min_peak_usd: float = Field(default=CANONICAL_DEFAULTS["daily_giveback_min_peak_usd"], ge=0.0)
    crowded_with_min_conf: float = Field(default=CANONICAL_DEFAULTS["crowded_with_min_conf"], ge=0.0, le=1.0)
    min_available_margin_pct: float = Field(default=CANONICAL_DEFAULTS["min_available_margin_pct"], ge=0.0, le=1.0)
    counter_regime_min_conf: float = Field(default=CANONICAL_DEFAULTS["counter_regime_min_conf"], ge=0.0, le=1.0)
    min_market_volume_usd: float = Field(default=CANONICAL_DEFAULTS["min_market_volume_usd"], ge=0.0)
    min_hip3_volume_usd: float = Field(default=CANONICAL_DEFAULTS["min_hip3_volume_usd"], ge=0.0)
    min_short_volume_usd: float = Field(default=CANONICAL_DEFAULTS["min_short_volume_usd"], ge=0.0)
    research_rescore_delta: float = Field(default=CANONICAL_DEFAULTS["research_rescore_delta"], ge=0.0, le=100.0)
    sl_buffer_bps: float = Field(default=CANONICAL_DEFAULTS["sl_buffer_bps"], ge=0.0, le=1000.0)
    # H4 (deep audit 2026-08-29): assumed maintenance-margin rate (%) for the
    # pre-trade liquidation-price gate. 0 disables the gate.
    liquidation_maint_margin_pct: float = Field(default=CANONICAL_DEFAULTS["liquidation_maint_margin_pct"], ge=0.0, le=20.0)
    tp_scale_fraction: float = Field(default=CANONICAL_DEFAULTS["tp_scale_fraction"], ge=0.0, le=1.0)
    against_funding_min_conf: float = Field(default=CANONICAL_DEFAULTS["against_funding_min_conf"], ge=0.0, le=1.0)
    against_funding_min_score: float = Field(default=CANONICAL_DEFAULTS["against_funding_min_score"], ge=0.0, le=100.0)
    chop_min_conf: float = Field(default=CANONICAL_DEFAULTS["chop_min_conf"], ge=0.0, le=1.0)
    chop_min_score: float = Field(default=CANONICAL_DEFAULTS["chop_min_score"], ge=0.0, le=100.0)
    chop_burst_min_score: float = Field(default=CANONICAL_DEFAULTS["chop_burst_min_score"], ge=0.0, le=100.0)
    strong_trend_threshold: float = Field(default=CANONICAL_DEFAULTS["strong_trend_threshold"], ge=0.0, le=1.0)
    trend_threshold: float = Field(default=CANONICAL_DEFAULTS["trend_threshold"], ge=0.0, le=1.0)
    neutral_threshold: float = Field(default=CANONICAL_DEFAULTS["neutral_threshold"], ge=0.0, le=1.0)
    max_atr_pct: float = Field(default=CANONICAL_DEFAULTS["max_atr_pct"], ge=0.0, le=100.0)
    max_spread_pct: float = Field(default=CANONICAL_DEFAULTS["max_spread_pct"], ge=0.0, le=100.0)
    sl_atr_mult: float = Field(default=CANONICAL_DEFAULTS["sl_atr_mult"], ge=0.0, le=50.0)
    # R12-C1: backup-SL clamp widths, manual-bracket TP mult, and the optional
    # lower confidence floor for regime-aligned entries (None = feature off).
    sl_ceiling_pct: float = Field(default=CANONICAL_DEFAULTS["sl_ceiling_pct"], ge=0.0, le=100.0)
    sl_floor_pct: float = Field(default=CANONICAL_DEFAULTS["sl_floor_pct"], ge=0.0, le=100.0)
    tp_atr_mult: float = Field(default=CANONICAL_DEFAULTS["tp_atr_mult"], ge=0.0, le=50.0)
    # R13-B4: HYPE-incident hard clamp on backup-SL width (%), and the P0-4
    # liquidation-buffer gate threshold in USD (0 disables the gate).
    sl_ceiling_hard_max_pct: float = Field(default=CANONICAL_DEFAULTS["sl_ceiling_hard_max_pct"], ge=0.0, le=100.0)
    liq_buffer_usd: float = Field(default=CANONICAL_DEFAULTS["liq_buffer_usd"], ge=0.0)
    aligned_min_conf: Optional[float] = Field(default=CANONICAL_DEFAULTS["aligned_min_conf"], ge=0.0, le=1.0)
    min_trend_score: float = Field(default=CANONICAL_DEFAULTS["min_trend_score"], ge=0.0, le=1.0)
    whale_size_multiplier: float = Field(default=CANONICAL_DEFAULTS["whale_size_multiplier"], ge=0.0)

    # ── lists (accepted as-is, element type not checked) ──────────────────
    coin_allowlist: list = Field(default_factory=lambda: _list_default("coin_allowlist"))
    coin_blocklist: list = Field(default_factory=lambda: _list_default("coin_blocklist"))
    hip3_dex_allowlist: list = Field(default_factory=lambda: _list_default("hip3_dex_allowlist"))
    hip3_dex_blocklist: list = Field(default_factory=lambda: _list_default("hip3_dex_blocklist"))
    # R12-C1: conviction-scaled size tiers, [[min_confidence, size_mult], ...].
    conviction_tiers: list = Field(default_factory=lambda: _list_default("conviction_tiers"))

    # ── nested objects (accepted as dicts, not deep-validated) ────────────
    dsl_exit: dict[str, Any] = Field(default_factory=lambda: _dict_default("dsl_exit"))
    runner_entry_gate: dict[str, Any] = Field(default_factory=lambda: _dict_default("runner_entry_gate"))
    plan_b: dict[str, Any] = Field(default_factory=lambda: _dict_default("plan_b"))
    atr_risk_sizing: dict[str, Any] = Field(default_factory=lambda: _dict_default("atr_risk_sizing"))
    regime_classifier: dict[str, Any] = Field(default_factory=lambda: _dict_default("regime_classifier"))
    regime_score: dict[str, Any] = Field(default_factory=lambda: _dict_default("regime_score"))
    # R13-B6: funding-crowding regime classifier knobs (hyperfeed.py). Five
    # keys: cache TTL, ±funding crowding threshold, per-class OI floors, and
    # the long-vs-short count dominance margin.
    funding_regime: dict[str, Any] = Field(default_factory=lambda: _dict_default("funding_regime"))
    debate_gate: dict[str, Any] = Field(default_factory=lambda: _dict_default("debate_gate"))
    debate_research: dict[str, Any] = Field(default_factory=lambda: _dict_default("debate_research"))
    signal_enforcement: dict[str, Any] = Field(default_factory=lambda: _dict_default("signal_enforcement"))
    momentum_continuation: dict[str, Any] = Field(default_factory=lambda: _dict_default("momentum_continuation"))
    candlestick_patterns: dict[str, Any] = Field(default_factory=lambda: _dict_default("candlestick_patterns"))
    capital_rotation: dict[str, Any] = Field(default_factory=lambda: _dict_default("capital_rotation"))
    gex_signal: dict[str, Any] = Field(default_factory=lambda: _dict_default("gex_signal"))
    shadow_signals: dict[str, Any] = Field(default_factory=lambda: _dict_default("shadow_signals"))
    momentum_reentry: dict[str, Any] = Field(default_factory=lambda: _dict_default("momentum_reentry"))
    runner_mover_surface: dict[str, Any] = Field(default_factory=lambda: _dict_default("runner_mover_surface"))
    memory_limits: dict[str, Any] = Field(default_factory=lambda: _dict_default("memory_limits"))
    llm_circuit_breaker: dict[str, Any] = Field(default_factory=lambda: _dict_default("llm_circuit_breaker"))
    coin_overrides: dict[str, Any] = Field(default_factory=lambda: _dict_default("coin_overrides"))
    # R12-C1: single-coin / daily drawdown halt thresholds.
    circuit_breaker: dict[str, Any] = Field(default_factory=lambda: _dict_default("circuit_breaker"))
    # R13-A1: perception scan-tick block. Nested because perception reads it
    # as `config["scan"]` (and TRIGGER_CONFIG["scan"] is itself a dict).
    scan: dict[str, Any] = Field(default_factory=lambda: _dict_default("scan"))
    # R13-B1: DSL state-file I/O tunables (dsl_exit.py). Five keys covering
    # the write throttle / force-reload TTL / policy cache TTL / save retry
    # / backoff base — all on the trade hot path.
    dsl_state_io: dict[str, Any] = Field(default_factory=lambda: _dict_default("dsl_state_io"))
    # R13-B2: exchange-SL mover tunables (executor.py). Two keys: per-coin
    # batchModify throttle and minimum bps-move to justify a cancel+replace.
    sl_move: dict[str, Any] = Field(default_factory=lambda: _dict_default("sl_move"))
    # R13-B3: risk-gate scoring thresholds (risk_gates.py). Eight keys cover
    # the market_regime_gate's counter-trend score bar plus the debate_gate
    # analyst2 / analyst5 thresholds.
    analyst_scoring: dict[str, Any] = Field(default_factory=lambda: _dict_default("analyst_scoring"))
    # R13-B4: executor execution-path cost / fee bookkeeping knobs
    # (executor.py). Two keys — HL perp taker fee in PERCENT and the
    # number of taker fills modeled per round trip. Defaults mirror the
    # existing module literals (0.025 / 2) verbatim.
    execution: dict[str, Any] = Field(default_factory=lambda: _dict_default("execution"))
    # R13-B7: free-signal-suite blocks — CBOE GEX cache/timeout, FINRA
    # short-volume thresholds, Binance whale-flow window/thresholds, GDELT
    # news-catalyst surge/timeout, and the HL whale-index OI/funding knobs.
    options_gex: dict[str, Any] = Field(default_factory=lambda: _dict_default("options_gex"))
    short_volume: dict[str, Any] = Field(default_factory=lambda: _dict_default("short_volume"))
    crypto_whale: dict[str, Any] = Field(default_factory=lambda: _dict_default("crypto_whale"))
    news_catalyst: dict[str, Any] = Field(default_factory=lambda: _dict_default("news_catalyst"))
    whale_index: dict[str, Any] = Field(default_factory=lambda: _dict_default("whale_index"))
    # R13-B8: trigger composite-score weights and thresholds (TRIGGER_CONFIG,
    # consumed by perception.scan_once). Nested because perception reads them
    # as config["weights"] / config["thresholds"]; snake_case canonical leaves
    # are mapped to the camelCase runtime keys by config.py helpers.
    trigger_weights: dict[str, Any] = Field(default_factory=lambda: _dict_default("trigger_weights"))
    trigger_thresholds: dict[str, Any] = Field(default_factory=lambda: _dict_default("trigger_thresholds"))
    # R13-B9: perception scan budget / pacing knobs (perception.py scan_once).
    # Twelve keys: cache size, market budget split, USD floors, universe
    # sweep, batch rate-limit pacing, thread-pool width, movers % cut-off and
    # future timeout. Legacy HERMES_* env vars remain the top-priority channel.
    scan_budget: dict[str, Any] = Field(default_factory=lambda: _dict_default("scan_budget"))
    # R13-B10: research-path LLM call knobs (research.py _call_openrouter /
    # _debate_direct). Eleven keys: gateway model/base URL, temperature,
    # normal/debate token budgets, read/connect timeouts, retry budget with
    # backoff base/cap, and continuation turns. OPENROUTER_MODEL /
    # OPENROUTER_BASE_URL env vars remain the top-priority channel.
    research_llm: dict[str, Any] = Field(default_factory=lambda: _dict_default("research_llm"))
    # R13-B10: research-path concurrency / prefetch knobs (research.py
    # _get_pool / _http / _signals_block / _parallel_prefetch). Nine keys:
    # shared pool width, httpx keepalive/total connection limits, signals
    # future timeout and the per-source fetch timeout family. Legacy
    # HERMES_RESEARCH_* env vars remain the top-priority channel.
    research_fetch: dict[str, Any] = Field(default_factory=lambda: _dict_default("research_fetch"))
    # R13-B11: memory equity quality-gate knobs (memory.py). Seven keys: the
    # track_daily_pnl partial-dex degraded-read filter (implausible fraction,
    # crash fraction, filter window, re-confirm streak), the exit-slip
    # lookback window / min samples, and the flush throttle. Legacy
    # HERMES_EQUITY_CRASH_DOWN_PCT / HERMES_MEMORY_FLUSH_THROTTLE_S env vars
    # remain the top-priority channel.
    memory_quality: dict[str, Any] = Field(default_factory=lambda: _dict_default("memory_quality"))
    # R13-B11: dashboard equity read-side quality-gate knobs (dashboard.py).
    # Four keys: equity-curve dip flag ratio / trailing window, summary
    # heartbeat staleness threshold, closed-trades dedup window. Legacy
    # HERMES_EQUITY_DIP_RATIO / HERMES_EQUITY_DIP_WINDOW /
    # HERMES_CLOSED_TRADES_DEDUP_MS env vars remain the top-priority channel.
    dashboard_equity: dict[str, Any] = Field(default_factory=lambda: _dict_default("dashboard_equity"))
    # R13-B12: HTTP-edge cache TTLs (dashboard.py / public.py / server.py).
    # Five keys: summary / equity-curve / closed-trades public poll endpoint
    # TTLs, per-coin research verdict cache TTL, and TTL-cache singleflight
    # waiter timeout. Legacy HERMES_SUMMARY_TTL_S / HERMES_EQUITY_CURVE_TTL_S
    # / HERMES_CLOSED_TRADES_TTL_S / HERMES_RESEARCH_HTTP_CACHE_S env vars
    # remain the top-priority channel.
    http_cache: dict[str, Any] = Field(default_factory=lambda: _dict_default("http_cache"))
    # R13-B13: Hyperliquid client-layer knobs (exchange.py / hl_client.py /
    # ws_client.py) — SDK timeout, fallback leverage, slippage caps, cache
    # TTLs/sizes, WS staleness/heartbeat/seq tolerances. Legacy
    # HERMES_HL_SDK_TIMEOUT_S / HERMES_DEFAULT_LEVERAGE /
    # HERMES_MAX_SLIPPAGE_PCT / HERMES_MAX_SLIPPAGE_CLOSE_PCT /
    # HERMES_META_TTL_S / HERMES_ATR_TTL_S / HERMES_CANDLE_CACHE_* /
    # HERMES_FUNDING_CACHE_TTL_S / HERMES_WS_* env vars remain the
    # top-priority channel.
    hl_client_io: dict[str, Any] = Field(default_factory=lambda: _dict_default("hl_client_io"))
    # R13-B13: Hyperliquid rate-limiter knobs (client/rate_limit.py). Seven
    # leaves: bucket refill/capacity, trading-path max wait, 429 retries,
    # opportunistic wait, and the shared-bucket / per-endpoint-gate switches.
    # Legacy HERMES_HL_RATE_* / HERMES_HL_429_RETRIES env vars remain the
    # top-priority channel.
    hl_rate_limit: dict[str, Any] = Field(default_factory=lambda: _dict_default("hl_rate_limit"))
    # ta_late_entry (deep audit 高危项, 2026-08-30): late-entry hard-gate
    # thresholds shared by the ta_filter pre-filter, the ta_late_entry_gate
    # pre-trade gate and the backtest engine (one source of truth).
    ta_late_entry: dict[str, Any] = Field(default_factory=lambda: _dict_default("ta_late_entry"))


# Keys whose out-of-range message predates the generic bounds table and is
# asserted on by operators/tests — keep the historical wording verbatim.
_SPECIAL_RANGE_KEYS = frozenset({
    "leverage", "max_concurrent", "min_ai_confidence", "equity_fraction_per_trade",
})

_TYPE_LABEL = {int: "int", float: "number", bool: "bool", str: "string", list: "list", dict: "object"}


def coerce_config_value(s: str) -> Any:
    """Terminal/CLI `set` type inference: bool → null → int → float → JSON → str."""
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except Exception:
            pass
    return s


def _expected_kind(annotation: Any) -> Any:
    """Map a model field annotation to the isinstance-check kind."""
    if annotation is int:
        return int
    if annotation is float:
        return float
    if annotation is bool:
        return bool
    if annotation is str:
        return str
    if annotation is list or get_origin(annotation) is list:
        return list
    if annotation is dict or get_origin(annotation) is dict:
        return dict
    return None


def _type_ok(kind: Any, val: Any) -> bool:
    if kind is int:
        return isinstance(val, int) and not isinstance(val, bool)
    if kind is float:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if kind is bool:
        return isinstance(val, bool)
    return isinstance(val, kind)


# ── D-FCFG-2 (deep audit 2026-08-28): deep validation for safety-critical ──
# nested blocks. Until now these blocks were accepted as bare dicts, so an
# operator could persist a malformed / out-of-range leaf (e.g.
# dsl_exit.max_loss_pct=-999 — a negative stop widens the loss limit 1000x;
# atr_stop.atr_mult="1.5x" — a crash-type that the .get() consumers treat as
# a fatal stop-construction input). The three blocks below gate trade RISK:
# DSL exit geometry, ATR risk sizing, and signal enforcement. Each leaf is
# spec'd with (kind, lo, hi); nested objects and list-of-objects recurse.
# A leaf spec is a tuple (kind, lo, hi); a dict spec maps sub-keys to specs
# (unknown sub-keys are rejected); list specs are ("list", item_spec) where
# item_spec is itself a dict spec validated for every element.

_NUM = (int, float)


def _num_leaf(lo: float, hi: float) -> tuple:
    return ("num", lo, hi)


# Percent leaves: percentages are expressed in the same units as the
# canonical defaults (max_loss_pct=0.4 means 0.4%, etc.). Bounds are
# deliberately generous sanity rails, not the runtime operating range.
_DSL_EXIT_SPEC: dict[str, Any] = {
    "max_loss_pct": _num_leaf(0.0, 25.0),
    "max_loss_roe_pct": _num_leaf(0.0, 200.0),
    "protect_pct": _num_leaf(0.0, 50.0),
    "retrace_threshold": _num_leaf(0.0, 1.0),
    "hard_timeout_minutes": _num_leaf(0.0, 100_000.0),
    "breakeven_trigger_pct": _num_leaf(0.0, 50.0),
    "breakeven_lock_pct": _num_leaf(0.0, 50.0),
    "stale_flat_timeout_minutes": _num_leaf(0.0, 100_000.0),
    "atr_stop": {
        "enabled": ("bool",),
        "atr_mult": _num_leaf(0.0, 20.0),
        "floor_pct": _num_leaf(0.0, 50.0),
        "ceiling_pct": _num_leaf(0.0, 100.0),
    },
    "noise_band": {
        "enabled": ("bool",),
        "atr_mult": _num_leaf(0.0, 20.0),
    },
    "consecutive_breaches_required": ("int", 1, 100),
    "breach_confirm_sec": _num_leaf(0.0, 600.0),
    "phase2_tiers": ("list", {
        "pct_above_entry": _num_leaf(0.0, 100.0),
        "retrace_threshold": _num_leaf(0.0, 1.0),
    }),
    "regime_aware": {
        "enabled": ("bool",),
        "trend_ride": {
            "protect_pct": _num_leaf(0.0, 50.0),
            "retrace_threshold": _num_leaf(0.0, 1.0),
            "phase2_tiers": ("list", {
                "pct_above_entry": _num_leaf(0.0, 100.0),
                "retrace_threshold": _num_leaf(0.0, 1.0),
            }),
        },
        "max_loss": {
            "trend": {
                "max_loss_pct": _num_leaf(0.0, 25.0),
                "max_loss_roe_pct": _num_leaf(0.0, 200.0),
            },
            "non_trend": {
                "max_loss_pct": _num_leaf(0.0, 25.0),
                "max_loss_roe_pct": _num_leaf(0.0, 200.0),
            },
        },
    },
}

_ATR_RISK_SIZING_SPEC: dict[str, Any] = {
    "enabled": ("bool",),
    "risk_per_trade_pct": _num_leaf(0.0, 1.0),
    "sizing_basis": ("enum", ("primary_stop", "dsl_stop", "atr_stop")),
    # Sizing v2 gray-release knobs (read with .get from the same block).
    "sizing_v2_enabled": ("bool",),
    "sizing_v2_cap_pct": _num_leaf(0.0, 1.0),
    # coin_overrides.<COIN> is a free-form per-coin map; only the leaves the
    # executor actually reads are spec-checked (sl_floor_pct), unknown
    # override leaves are accepted (plugin/extension channel).
    "coin_overrides": ("dict_of", {
        "sl_floor_pct": _num_leaf(0.0, 50.0),
    }),
}

_SIGNAL_ENFORCEMENT_SPEC: dict[str, Any] = {
    "enabled": ("bool",),
    "veto": ("bool",),
    "boost": ("bool",),
    "gex_veto": ("bool",),
    "boost_bar_delta": ("int", 0, 1000),
    "whale_window_min": ("int", 0, 100_000),
    "whale_veto_min_usd": _num_leaf(0.0, 1_000_000_000.0),
    "whale_boost_min_usd": _num_leaf(0.0, 1_000_000_000.0),
}

_NESTED_BLOCK_SPECS: dict[str, dict[str, Any]] = {
    "dsl_exit": _DSL_EXIT_SPEC,
    "atr_risk_sizing": _ATR_RISK_SIZING_SPEC,
    "signal_enforcement": _SIGNAL_ENFORCEMENT_SPEC,
    "ta_late_entry": {
        # off = gate absent; shadow = record/metrics only (gray release);
        # enforce = late entries are actually blocked.
        "mode": ("enum", ("off", "shadow", "enforce")),
        # 4h hard veto: RSI extremes OR price extension in ATR units.
        "rsi_ob": _num_leaf(50.0, 100.0),
        "rsi_os": _num_leaf(0.0, 50.0),
        "ext_ob": _num_leaf(0.0, 20.0),
        "ext_os": _num_leaf(-20.0, 0.0),
        # Trend exception: relax the veto limits in a strong aligned trend.
        "trend_relax_enabled": ("bool",),
        "adx_trend_threshold": _num_leaf(0.0, 100.0),
        "rsi_ob_relaxed": _num_leaf(50.0, 100.0),
        "rsi_os_relaxed": _num_leaf(0.0, 50.0),
        "ext_ob_relaxed": _num_leaf(0.0, 20.0),
        "ext_os_relaxed": _num_leaf(-20.0, 0.0),
        # Multi-timeframe: 15m RSI continuation override.
        "mtf_enabled": ("bool",),
        "rsi15m_ob": _num_leaf(50.0, 100.0),
        "rsi15m_os": _num_leaf(0.0, 50.0),
        # Data requirements / fetch sizing.
        "min_bars_4h": ("int", 10, 500),
        "min_bars_15m": ("int", 5, 500),
        "fetch_bars": ("int", 30, 1000),
        "shadow_log_path": ("str",),
    },
}


def _validate_nested_spec(path: str, val: Any, spec: Any, errors: list[str]) -> None:
    """Recursively validate one nested-block value against a leaf/dict/list
    spec. Error paths are dotted (``dsl_exit.atr_stop.atr_mult``).

    A bare ``dict`` spec (sub-key → spec mapping) is the same as the
    ``("dict", {...})`` tuple form — nested object specs are written bare,
    container specs need the explicit tag."""
    if isinstance(spec, dict):
        spec = ("dict", spec)
    kind = spec[0]
    if kind == "dict":
        if not isinstance(val, dict):
            errors.append(f"{path}: expected object, got {type(val).__name__}")
            return
        for k, v in val.items():
            if k not in spec[1]:
                errors.append(f"{path}.{k}: unknown key")
            else:
                _validate_nested_spec(f"{path}.{k}", v, spec[1][k], errors)
    elif kind == "list":
        if not isinstance(val, list):
            errors.append(f"{path}: expected list, got {type(val).__name__}")
            return
        for i, item in enumerate(val):
            _validate_nested_spec(f"{path}[{i}]", item, ("dict", spec[1]), errors)
    elif kind == "dict_of":
        # Free-form map (e.g. coin_overrides keyed by coin); map keys are
        # arbitrary strings. Only the leaves the runtime actually reads are
        # spec-checked; unknown override leaves are silently ignored — they
        # are the documented forward-compat / plugin extension channel
        # (RISK_OVERHAUL_2026-08-26 ships e.g. atr_stop_floor_pct there).
        if not isinstance(val, dict):
            errors.append(f"{path}: expected object, got {type(val).__name__}")
            return
        leaf_specs = spec[1]
        for mk, mv in val.items():
            if not isinstance(mk, str):
                errors.append(f"{path}: keys must be strings, got {type(mk).__name__}")
                continue
            if not isinstance(mv, dict):
                errors.append(
                    f"{path}.{mk}: expected object, got {type(mv).__name__}"
                )
                continue
            for lk, lv in mv.items():
                if lk in leaf_specs:
                    _validate_nested_spec(
                        f"{path}.{mk}.{lk}", lv, leaf_specs[lk], errors
                    )
    elif kind == "bool":
        if not isinstance(val, bool):
            errors.append(f"{path}: expected bool, got {type(val).__name__}")
    elif kind == "int":
        if not isinstance(val, int) or isinstance(val, bool):
            errors.append(f"{path}: expected int, got {type(val).__name__}")
        elif val < spec[1] or val > spec[2]:
            errors.append(f"{path}: must be between {spec[1]} and {spec[2]}")
    elif kind == "num":
        if isinstance(val, bool) or not isinstance(val, _NUM):
            errors.append(f"{path}: expected number, got {type(val).__name__}")
        elif val < spec[1] or val > spec[2]:
            errors.append(
                f"{path}: must be between {spec[1]} and {spec[2]} (got {val})"
            )
    elif kind == "enum":
        if not isinstance(val, str) or val not in spec[1]:
            errors.append(f"{path}: must be one of {list(spec[1])}, got {val!r}")
    elif kind == "str":
        if not isinstance(val, str):
            errors.append(f"{path}: expected string, got {type(val).__name__}")


def validate_config_updates(updates: dict[str, Any], *, strict_keys: bool = True) -> list[str]:
    """Validate a partial config update. Returns a list of error strings.

    ``strict_keys=True`` (both web write paths — POST /api/dashboard/config
    and POST /api/agent/config — plus terminal ``set`` and the CLI): unknown
    keys are rejected. ``strict_keys=False`` is retained for schema-level
    callers that deliberately preserve caller-policy keys; as of D-FCFG-4
    (deep audit 2026-08-28) no HTTP write path uses it.
    """
    errors: list[str] = []
    fields = _ConfigPatch.model_fields
    for key, val in updates.items():
        if key not in CANONICAL_DEFAULTS:
            if strict_keys:
                errors.append(f"unknown key: {key}")
            continue
        field = fields.get(key)
        if field is None:
            # ``_comment`` (underscore fields can't live on the model) —
            # accepted as-is, matching historical behavior.
            continue
        kind = _expected_kind(field.annotation)
        if kind is not None and not _type_ok(kind, val):
            errors.append(f"{key}: expected {_TYPE_LABEL[kind]}, got {type(val).__name__}")
            continue
        if key in _SPECIAL_RANGE_KEYS:
            continue
        # Generic numeric bounds, reflected from the Field(ge=/le=) metadata.
        if kind in (int, float):
            lo = hi = None
            for meta in field.metadata:
                g = getattr(meta, "ge", None)
                if g is None:
                    g = getattr(meta, "gt", None)
                t = getattr(meta, "le", None)
                if t is None:
                    t = getattr(meta, "lt", None)
                if g is not None:
                    lo = g
                if t is not None:
                    hi = t
            if lo is not None and val < lo:
                errors.append(f"{key}: must be >= {lo}")
            elif hi is not None and val > hi:
                errors.append(f"{key}: must be <= {hi}")

    # Dedicated range checks with historical wording.
    if "leverage" in updates and isinstance(updates["leverage"], int) and not isinstance(updates["leverage"], bool):
        if updates["leverage"] < 1 or updates["leverage"] > 50:
            errors.append("leverage: must be 1\u201350")
    if "max_concurrent" in updates and isinstance(updates["max_concurrent"], int) and not isinstance(updates["max_concurrent"], bool):
        if updates["max_concurrent"] < 0:
            errors.append("max_concurrent: must be >= 0")
    v = updates.get("min_ai_confidence")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not (0.0 <= v <= 1.0):
            errors.append("min_ai_confidence: must be 0.0\u20131.0")
    v = updates.get("equity_fraction_per_trade")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not (0.0 < v <= 1.0):
            errors.append("equity_fraction_per_trade: must be > 0 and <= 1.0")

    # ── P0-2: mode enum + sane-floors for safety-critical thresholds ─────
    # The mode field used to be a free-form string — an operator typo
    # ("mode=ON" / "mode=ENABLED" / "mode=live ") silently left the bot in
    # OFF because the loop only acts on the canonical three. Reject anything
    # else at the schema layer so a typo is loud, not silent.
    if "mode" in updates:
        _MODE_ENUM = ("OFF", "LIVE", "SHADOW")
        if updates["mode"] not in _MODE_ENUM:
            errors.append(
                f"mode: must be one of {list(_MODE_ENUM)}, got {updates['mode']!r}"
            )

    # Sane-floors: each of these thresholds can disable a safety gate when
    # set to an absurd value. Typos like max_daily_loss_usd=0 (kills the
    # kill switch — any negative day still passes the `daily_pnl > 0`
    # check), max_total_notional_pct=0 (caps every trade at $0 notional,
    # silent no-op), or max_trade_notional_usd=0 (disables the per-trade
    # cap entirely) all silently neuter a layer of defence. We reject
    # them at the schema layer; if the operator genuinely wants to
    # disable a gate they can set its key to a sane positive value
    # (P0-3 will introduce an explicit disable flag for that class).
    #
    # The floors are deliberately generous so they only catch typos, not
    # legitimate low-risk configurations.
    if "max_daily_loss_usd" in updates and isinstance(
        updates["max_daily_loss_usd"], (int, float)
    ) and not isinstance(updates["max_daily_loss_usd"], bool):
        # Daily loss must be negative (it's a *cap* on losses, not a
        # target) AND not absurdly large in magnitude — anything more
        # negative than -100k per day is almost certainly a typo (the
        # account is 1k USDC testnet / 10k USDC live). Allowed range:
        # [-100_000, 0].
        v = float(updates["max_daily_loss_usd"])
        if v > 0:
            errors.append("max_daily_loss_usd: must be <= 0 (it's a loss cap)")
        elif v < -100_000:
            errors.append(
                "max_daily_loss_usd: must be >= -100000 (sanity floor; "
                "values more negative than that almost certainly indicate a typo)"
            )

    if "max_total_notional_pct" in updates and isinstance(
        updates["max_total_notional_pct"], (int, float)
    ) and not isinstance(updates["max_total_notional_pct"], bool):
        # Equity-relative cap. A value of 0 silently kills the gate (the
        # gate treats 0 as "no cap" → no block); < 1 (i.e. < 100% of
        # equity) is the normal range; > 50 is reckless. Allowed range:
        # [0.5, 50]. Set 0 to disable (the gate's own convention).
        v = float(updates["max_total_notional_pct"])
        if 0 < v < 0.5:
            errors.append(
                "max_total_notional_pct: must be >= 0.5 (values <0.5 "
                "effectively freeze the account — likely a typo); use 0 "
                "to disable the cap explicitly"
            )

    if "max_trade_notional_usd" in updates and isinstance(
        updates["max_trade_notional_usd"], (int, float)
    ) and not isinstance(updates["max_trade_notional_usd"], bool):
        # Per-trade notional cap. 0 = disabled (gate convention).
        # Below the HL minimum order size ($10) the gate is effectively
        # the floor anyway, so anything < 10 in the positive range is a
        # typo. Allowed range: [0, MAX_TRADE_NOTIONAL_USD].
        v = float(updates["max_trade_notional_usd"])
        if 0 < v < 10.0:
            errors.append(
                "max_trade_notional_usd: must be >= 10 (HL minimum order "
                "size); use 0 to disable the cap explicitly"
            )

    # ── P0-3: FORBIDDEN_OVERRIDE — force-execute / bypass switches ────────
    # Six config flags can disable a safety gate by themselves:
    #   composite_force_execute   — bypasses the confidence floor when
    #                                composite score is high
    #   breakout_force_execute    — bypasses AI confirmation on breakout
    #                                triggers (volume + price)
    #   whale_force_execute       — bypasses AI confirmation on whale
    #                                accumulation signals
    #   ta_sidestep_force_execute — bypasses AI confirmation on the TA
    #                                sidestep path (enough slow-burn
    #                                triggers plus a composite/momentum
    #                                breakout)
    #   whale_regime_bypass       — lets a whale signal clear the
    #                                counter-regime gate
    #   spread_gate_fail_open     — makes the spread/impact gate default
    #                                to PASS when its data is unavailable
    #                                (the only one of the six that turns
    #                                a fail-CLOSED gate into fail-OPEN)
    #
    # Each of these is a "skip a check" lever. Enabling any of them
    # without an explicit AI agreement is a single-keystroke path to
    # losing money: the operator can flip a bool in the dashboard and
    # the next trade is unprotected.
    #
    # The contract:
    #   - When ANY of the six is set true, override_requires_ai must
    #     ALSO be true in the SAME update. (splitting them across two
    #     writes is allowed but the resulting state must have both
    #     true; we check the merged state below by reading the current
    #     config when available.)
    #   - The runtime caller (executor / perception / spread gate)
    #     must write a force_override_armed audit line whenever it
    #     consults a force-execute switch in the armed state, so
    #     post-trade review can see "this trade was force-executed
    #     under override_requires_ai".
    _FORCE_OVERRIDE_KEYS = (
        "composite_force_execute", "breakout_force_execute",
        "whale_force_execute", "ta_sidestep_force_execute",
        "whale_regime_bypass", "spread_gate_fail_open",
    )
    for fkey in _FORCE_OVERRIDE_KEYS:
        if updates.get(fkey) is True:
            if not updates.get("override_requires_ai", False):
                errors.append(
                    f"{fkey}=true requires override_requires_ai=true in "
                    f"the same update (FORBIDDEN_OVERRIDE — never arm a "
                    f"force-execute switch without explicit AI agreement)"
                )

    # ── D-FCFG-2: deep validation of safety-critical nested blocks ────────
    # The three blocks gate trade risk (DSL exit geometry, ATR sizing,
    # signal enforcement). Top-level typing only proved they were dicts;
    # a malformed leaf (e.g. dsl_exit.max_loss_pct=-999, atr_mult="1.5x")
    # used to persist and reach the executor. Recurse per the leaf specs.
    for _block, _spec in _NESTED_BLOCK_SPECS.items():
        if _block in updates:
            _validate_nested_spec(
                _block, updates[_block], ("dict", _spec), errors
            )

    return errors


_FORCE_OVERRIDE_KEYS_FOR_GATE = (
    "composite_force_execute", "breakout_force_execute",
    "whale_force_execute", "ta_sidestep_force_execute",
    "whale_regime_bypass", "spread_gate_fail_open",
)


def validate_forbidden_overrides(cfg: dict[str, Any]) -> list[str]:
    """R11-E1: run only the FORBIDDEN_OVERRIDE contract on a whole-config view.

    ``validate_config_updates`` is a *patch* gate — it only inspects what the
    caller is writing, not what is already on disk. The four hot-load write
    paths in ``config_store`` (read-back of a hand-edited JSON, direct
    ``write_agent_config`` calls, ``restore_backup`` / ``restore_snapshot``)
    need a whole-view safety net, but applying the patch gate's full suite
    (mode enum, safety floors) to the merged state would false-reject
    legitimate historical values ("mode" once accepted any string before
    the P0-2 enum was added) and orphan the operator's .bak.

    This helper runs the *one* check that is safe and necessary on a whole
    view: the FORBIDDEN_OVERRIDE contract — any force-execute switch armed
    without ``override_requires_ai=true`` is refused. That contract is
    independent of historical values because the force-execute keys only
    exist as booleans (a legacy "ON" string in a JSON file is a *type*
    failure, caught by ``_validate_cfg_value`` upstream of this call).
    """
    errors: list[str] = []
    for fkey in _FORCE_OVERRIDE_KEYS_FOR_GATE:
        if cfg.get(fkey) is True and cfg.get("override_requires_ai") is not True:
            errors.append(
                f"{fkey}=true requires override_requires_ai=true in "
                f"the same config (FORBIDDEN_OVERRIDE — never arm a "
                f"force-execute switch without explicit AI agreement)"
            )
    return errors
