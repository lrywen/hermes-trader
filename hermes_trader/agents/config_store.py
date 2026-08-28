"""Read/write the agent config at .agent-config.json.

Single source of truth for agent configuration. All modules MUST read
parameters through :func:`cfg_get` (or :func:`read_agent_config` for the raw
dict) rather than scattering ``.get(key, hardcoded_default)`` calls whose
fallback values can drift from the canonical config file.

Canonical defaults (``CANONICAL_DEFAULTS``) mirror the production
``.agent-config.json``. When a key is absent from the config file, the
canonical default is used. Environment variables prefixed with
``HERMES_CFG_`` override individual values (double-underscore separates
nested keys, e.g. ``HERMES_CFG_DSL_EXIT__PROTECT_PCT``).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Use absolute path based on this file's location (hermes-trader project root)
# __file__ = .../hermes-trader/hermes_trader/agents/config_store.py
# Go up 3 levels: agents/ -> hermes_trader/ -> hermes-trader/
# Override with HERMES_AGENT_CONFIG_FILE when deploying behind a mounted volume.
_CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.environ.get(
    "HERMES_AGENT_CONFIG_FILE",
    os.path.join(_CONFIG_DIR, ".agent-config.json"),
)
_CONFIG_LOCK_PATH = CONFIG_PATH + ".lock"
_BACKUP_PATH = CONFIG_PATH + ".bak"

# ── P1-10: mtime/size cache for the raw config read ─────────────────────
# read_agent_config() is called on EVERY coin path (research/gates/executor)
# — dozens of times per scan cycle. Each call took a flock(LOCK_SH) + open +
# full json.load + deep_merge. The config only changes on operator writes, so
# cache the parsed dict keyed by (mtime_ns, size): a cheap stat() decides
# whether the heavy path is needed. write_agent_config() invalidates the
# cache explicitly; cross-process changes (dashboard writes another file?)
# are detected by the mtime/size stat. Lock guards the cache itself.
_RAW_CACHE: Optional[dict[str, Any]] = None
_RAW_CACHE_SIG: Optional[tuple] = None
_RAW_CACHE_LOCK = threading.Lock()


def _config_sig() -> Optional[tuple]:
    """Return (mtime_ns, size) for the config file, or None if missing."""
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _invalidate_raw_cache() -> None:
    """Drop the cached raw config (call after a local write)."""
    global _RAW_CACHE, _RAW_CACHE_SIG
    with _RAW_CACHE_LOCK:
        _RAW_CACHE = None
        _RAW_CACHE_SIG = None

# ---------------------------------------------------------------------------
# Canonical defaults — MUST stay in sync with .agent-config.json.
# These are the fallback values used when a key is missing from the config
# file. They replace the dozens of scattered ``.get(key, <random number>)``
# calls whose defaults had drifted from production (e.g. max_daily_loss_usd
# fell back to -100 in one place while production uses -30).
# ---------------------------------------------------------------------------
CANONICAL_DEFAULTS: dict[str, Any] = {
    "mode": "OFF",
    "enable_crypto": True,
    "enable_hip3": True,
    "equity_fraction_per_trade": 0.2,
    "leverage": 10,
    "max_trade_notional_usd": 800,
    "tp_scale_fraction": 0.5,
    "max_concurrent": 10,
    "max_total_notional_pct": 10.0,
    "max_daily_loss_usd": -30,
    "daily_giveback_halt_pct": 0.35,
    "daily_giveback_min_peak_usd": 25.0,
    "crowded_with_min_conf": 0.8,
    "min_available_margin_pct": 0.1,
    "cooldown_min": 30,
    "research_cooldown_min": 15,
    "held_research_interval_min": 10,
    "min_ai_confidence": 0.7,
    "counter_regime_min_conf": 0.8,
    "max_crypto_long_correlated": 3,
    "min_market_volume_usd": 5_000_000,
    "min_hip3_volume_usd": 5_000_000,
    "min_short_volume_usd": 50_000_000,
    "coin_allowlist": [],
    "coin_blocklist": ["TON", "TRX"],
    "hip3_dex_allowlist": ["xyz"],
    "hip3_dex_blocklist": [],
    "dsl_exit": {
        "max_loss_pct": 0.4,
        "max_loss_roe_pct": 5.0,
        "protect_pct": 1.25,
        "retrace_threshold": 0.2,
        "hard_timeout_minutes": 1800.0,
        "breakeven_trigger_pct": 0.0,
        "breakeven_lock_pct": 0.0,
        "stale_flat_timeout_minutes": 480.0,
        "atr_stop": {
            "enabled": False,
            "atr_mult": 1.5,
            "floor_pct": 1.0,
            "ceiling_pct": 4.0,
        },
        # R12-C1: noise band tolerates a pull-back of atr_mult × entry ATR%
        # below the floor before an exit fires (sub-first-tier only); was
        # implicit via .get("noise_band", {}) in executor/dsl_exit.
        "noise_band": {
            "enabled": False,
            "atr_mult": 1.0,
        },
        # R12-C1: floor-breach confirmation. Was implicit: executor built
        # ExitPolicy with hardcoded defaults (1 / 0.0).
        "consecutive_breaches_required": 1,
        "breach_confirm_sec": 0.0,
        "phase2_tiers": [
            {"pct_above_entry": 8.0, "retrace_threshold": 0.35},
            {"pct_above_entry": 15.0, "retrace_threshold": 0.4},
        ],
        "regime_aware": {
            "enabled": True,
            "trend_ride": {
                "protect_pct": 3.0,
                "retrace_threshold": 0.55,
                "phase2_tiers": [
                    {"pct_above_entry": 3.0, "retrace_threshold": 0.55},
                    {"pct_above_entry": 8.0, "retrace_threshold": 0.45},
                    {"pct_above_entry": 15.0, "retrace_threshold": 0.4},
                ],
            },
            "max_loss": {
                "trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0},
                "non_trend": {"max_loss_pct": 0.4, "max_loss_roe_pct": 5.0},
            },
        },
    },
    "force_execute_composite": 30,
    "composite_force_execute": False,
    "ta_sidestep_force_execute": True,
    "ta_sidestep_min_slow_burn_count": 99,
    "force_execute_slow_burn_count": 2,
    "conviction_sizing": False,
    # R12-C1: legacy conviction sizing ladder (only consulted when
    # conviction_sizing=true). [[min_confidence, size_multiplier], ...];
    # was implicit via executor._DEFAULT_CONVICTION_TIERS.
    "conviction_tiers": [[0.80, 1.5], [0.65, 1.0], [0.0, 0.7]],
    "whale_regime_bypass": False,
    "whale_force_execute": False,
    "whale_size_multiplier": 1.0,
    "block_counter_trend_bypass": True,
    "trend_surface_enabled": True,
    "loss_cooldown_min": 180,
    "min_ai_close_hold_min": 25,
    "breakout_force_execute": False,
    "sl_atr_mult": 1.5,
    # R12-C1: backup stop clamp width (%) and manual/TP bracket ATR mult.
    # Was implicit via executor module constants (_DEFAULT_SL_CEILING_PCT=3.0,
    # _DEFAULT_SL_FLOOR_PCT=1.2) and server.py tp default 1.0. sl_floor_pct
    # has a per-coin override via atr_risk_sizing.coin_overrides.<coin>.
    "sl_ceiling_pct": 3.0,
    "sl_floor_pct": 1.2,
    "tp_atr_mult": 1.0,
    "min_trend_score": 0.55,
    # Regime classifier thresholds (chop / against-funding conviction bars)
    "chop_min_conf": 0.75,
    "chop_min_score": 55.0,
    # P1-4: momentum-burst bypass in chop requires at least this composite score
    "chop_burst_min_score": 20.0,
    "against_funding_min_conf": 0.85,
    "against_funding_min_score": 60.0,
    # Regime strength label thresholds
    "strong_trend_threshold": 0.70,
    "trend_threshold": 0.55,
    "neutral_threshold": 0.40,
    # Pre-trade volatility / spread gates (previously env-only)
    "max_atr_pct": 15.0,
    "max_spread_pct": 1.0,
    "spread_gate_fail_open": False,
    "runner_entry_gate": {
        "enabled": True,
        "allow_shorts": False,
        "bypass_sidestep_overrides": True,
        "min_confidence": 0.7,
        "min_composite": 30.0,
        "min_hip3_composite": 50.0,
        "min_short_confidence": 0.72,
        "min_short_composite": 25.0,
        "mover_min_confidence": 0.72,
        "mover_min_composite": 20.0,
        # R12-C1: pullback-long bypass admits uptrend longs that have pulled
        # back to a lower-risk zone. Off by default; was implicit via
        # gate.get("pullback_long") hardcoded defaults in executor.
        "pullback_long": {
            "enabled": False,
            "min_composite": 20.0,
            "max_rsi": 70.0,
            "max_extension_atr": 2.0,
            "min_slow_burn": 1,
            "shadow_mode": False,
        },
    },
    "plan_b": {
        "enabled": True,
        "rsi_low": 40.0,
        "rsi_high": 60.0,
        "size_mult": 0.5,
    },
    "atr_risk_sizing": {
        "enabled": True,
        "risk_per_trade_pct": 0.02,
        "sizing_basis": "primary_stop",
        # R12-C1: per-coin overrides for the ATR sizing / SL floor params
        # (e.g. {"HYPE": {"sl_floor_pct": 1.5}}). Empty by default; was
        # implicit via .get("coin_overrides", {}) in executor.
        "coin_overrides": {},
    },
    "regime_classifier": {
        "fast_ema": 20,
        "slow_ema": 30,
        "slope_threshold": 0.002,
        "chop_adx_max": 20.0,
    },
    "debate_gate": {
        "enabled": True,
        "min_agreement": 0.6,
        "min_agree_count": 3,
        # R12-C1: when true, a bull/bear split defaults to a third-analyst
        # tiebreak instead of fail-closed disagreement. Was implicit via
        # debate_cfg.get("analyst3_default", False) in risk_gates.
        "analyst3_default": False,
    },
    # Native in-process multi-perspective research debate. Off by default —
    # when enabled, research() runs bull/bear LLM calls in parallel plus an
    # arbiter synthesis with a hard latency cap and a single-LLM fallback on
    # any failure.
    "debate_research": {
        "enabled": False,
        "max_latency_s": 15.0,
        "cache_ttl_s": 300.0,
        # P2-2: max entries in the in-process verdict cache (composite key of
        # coin + score bucket + trigger hash); oldest-expiry evicted past cap.
        "cache_max_entries": 128,
        "parallel": True,
        "use_structured_output": True,
    },
    "signal_enforcement": {
        "enabled": True,
        "veto": True,
        "boost": True,
        "gex_veto": True,
        "boost_bar_delta": 4,
        "whale_window_min": 15,
        "whale_veto_min_usd": 250000,
        "whale_boost_min_usd": 250000,
    },
    # 动量延续因子（趋势中继回调入场）
    "momentum_continuation": {
        "enabled": False,
        "log_near_miss": True,
        "min_trend_pct": 8.0,
        "max_pullback_pct": 6.0,
        "weight": 0.4,
    },
    # K线形态识别
    "candlestick_patterns": {
        "enabled": False,
        "wick_body_ratio": 2.0,
        "context_lookback": 6,
        "context_pct": 1.5,
    },
    # 人工干预需AI二次研判
    "override_requires_ai": True,
    # 鲸鱼扫描绕过趋势检查
    "whale_scan_bypass": False,
    # 持仓评分变化豁免研判冷却
    "research_rescore_delta": 0,
    # 资金轮动（弱势仓换强势标的）
    "capital_rotation": {
        "enabled": False,
        "shadow_mode": False,
        "min_candidate_composite": 40.0,
        "min_hold_minutes": 30,
        "protect_winner_roe_pct": 3.0,
    },
    # GEX（Gamma Exposure）信号
    "gex_signal": {
        "enabled": True,
        "shadow_mode": False,
        "caution_near_wall_pct": 10.0,
    },
    # 影子信号（只记录不执行）
    "shadow_signals": {
        "enabled": True,
        "gex": True,
        "short_volume": True,
        "crypto_whale": True,
        "news": True,
        "whale_window_min": 15,
    },
    # 动量回补（趋势回归重新入场）
    "momentum_reentry": {
        "enabled": False,
        "reclaim_pct": 1.0,
        "min_composite": 30,
    },
    # Runner/Mover 表面扫描（涨幅榜筛选）
    "runner_mover_surface": {
        "enabled": True,
        "min_crypto_24h_pct": 10.0,
        "min_hip3_24h_pct": 8.0,
        "min_volume_usd": 5_000_000,
    },
    # P2-3: in-process memory retention limits for AgentMemory. Previously
    # hardcoded module constants; operators can now resize the JSON cache /
    # event-log rebuild windows without code changes.
    "memory_limits": {
        "max_perceptions": 500,
        "max_analyses": 200,
        "max_trades": 100,
        "max_closes": 500,
        # R9/P3-4: age-based retention in days for the time-bounded lists.
        # 0 disables age eviction (trades are an audit record — count-capped
        # only). Records without a usable timestamp are never age-evicted.
        "max_age_days": {
            "perceptions": 30,
            "analyses": 30,
            "trades": 0,
        },
    },
    # P2-3: bps the exchange backup stop sits behind the DSL floor (executor
    # SL ratchet coordination); and the funding-rate history lookback window
    # in hours (research display / against-funding context).
    "sl_buffer_bps": 10.0,
    "funding_lookback_hours": 24,
    # R13-B2: dynamic exchange-SL mover (executor.py L304-311) tunables.
    # Two knobs control how aggressively the trailing SL follows the DSL
    # floor in Phase 2:
    #   * min_interval_sec — per-coin throttle on batchModify (avoid
    #     spamming HL cancel+replace every tick / respect rate limit)
    #   * min_bps — minimum relative-to-entry move (in bps) that
    #     justifies a cancel+replace (filters micro-ratchets)
    # Were hardcoded module-level constants (_SL_MOVE_MIN_INTERVAL_SEC=30.0,
    # _SL_MOVE_MIN_BPS=15.0) at executor.py L304/307; perception / R12
    # audit flagged them as unobservable + not env-overridable + not
    # dashboard-dumpable. sl_buffer_bps above is already plumbed via
    # cfg_get at executor L2389; this block brings the other two knobs
    # into parity. Defaults match the existing literals verbatim.
    "sl_move": {
        "min_interval_sec": 30.0,
        "min_bps": 15.0,
    },
    # R9/P2-3: news gate freshness window (days) and the short-TTL Brave
    # headline cache (seconds). Were hardcoded module constants in research.py.
    "news_freshness_days": 2,
    "news_cache_ttl_s": 120,
    # P3-2: research-path LLM circuit breaker. After fail_threshold consecutive
    # hard failures (non-success HTTP / network error) the breaker opens for
    # cooldown_s and _call_openrouter short-circuits to "" so a dead upstream
    # can't pile up 60s-timeout calls across every coin each tick; callers
    # already degrade gracefully on empty. Mirrors the dashboard chat breaker.
    "llm_circuit_breaker": {
        "fail_threshold": 3,
        "cooldown_s": 300,
    },
    # Per-coin parameter overrides; deep-merged on top of the base config by
    # with_coin_overrides() / executor. Empty by default.
    "coin_overrides": {},
    # R12-C1: layered trading circuit breakers (executor post-close path).
    # A single coin's realized spot loss >= single_coin_loss_pct halts new
    # entries in that coin for single_coin_halt_min; cumulative daily PnL
    # loss >= daily_loss_pct of start-of-day equity halts ALL entries for
    # daily_halt_min. Thresholds were implicit cfg_get(..., default=) values
    # in executor.py and invisible to operators / config audit. Set a halt
    # duration to 0 to disable that layer.
    "circuit_breaker": {
        "single_coin_loss_pct": 3.0,
        "single_coin_halt_min": 60.0,
        "daily_loss_pct": 5.0,
        "daily_halt_min": 120.0,
    },
    # R13-A1: perception scan-tick block (TRIGGER_CONFIG["scan"], perception.py
    # L216-269). Previously implicit: the keys lived only in the module-level
    # TRIGGER_CONFIG dict and perception read them via `config["scan"][key]`
    # without ever consulting read_agent_config() / cfg_get. The MCP server
    # therefore had to hard-code its own defaults (180s / 20) which silently
    # drifted from production (5m / 54). Registering the block here makes the
    # values configurable, env-overridable, dashboard-dumpable, and — most
    # importantly — gives the MCP server a single source of truth to read
    # from. Defaults mirror TRIGGER_CONFIG verbatim; behaviour is unchanged.
    "scan": {
        "minCompositeScore": 54,
        "candleInterval": "5m",
        "candleCount": 100,
        "cacheTtlMs": 50_000,
        "cacheTtlMs1h": 600_000,
        "evaluateClosedBarsOnly": True,
        "postCloseForceRefreshMs": 15_000,
    },
    # R13-B1: DSL state-file I/O tunables (dsl_exit.py L65/77/86/1061/1063).
    # The five knobs (process-wide save throttle, dashboard force-reload TTL,
    # ExitPolicy cache TTL, save retry attempts, save backoff base) were
    # previously read only via os.environ.get(HERMES_DSL_*, <literal>) at
    # module-load. They never appeared in CANONICAL_DEFAULTS, so MCP
    # server / dashboard dump / validate_config_updates could neither
    # observe nor override them — a real operational blind spot on the
    # hot path (every WS mid tick reads them). Defaults match the existing
    # literals verbatim; behaviour is unchanged. Legacy HERMES_DSL_* env
    # vars continue to take precedence (operator override) and the
    # canonical env route (HERMES_CFG_DSL_STATE_IO__*) also works.
    "dsl_state_io": {
        "save_min_interval_sec": 2.0,
        "force_load_ttl_s": 1.0,
        "policy_cache_ttl_s": 5.0,
        "save_max_attempts": 3,
        "save_backoff_base_sec": 0.1,
    },
    # R13-B3: risk-gate scoring thresholds (risk_gates.py). Eight keys cover
    # the market_regime_gate's counter-trend score bar plus the debate_gate
    # analyst2 / analyst5 thresholds — all of which used to be hardcoded
    # module-level literals. They were therefore not env-overridable, not
    # dashboard-dumpable, and not auditable via the canonical schema. The
    # canonical default values match the existing literals verbatim so the
    # runtime bar is unchanged; only the *path* changes (cfg_get with module
    # default). Subagent audit had flagged the 50.0 / 60.0 "implicit
    # mismatch" at L474/L484 as a drift candidate, but on close reading
    # the two numbers are NOT inconsistent: 50.0 is the *normal*
    # counter-trend bar (L474 default) and 60.0 is the *elevated*
    # against-funding bar (L488 override). The L474 literal was the actual
    # dead default — it now resolves through cfg_get to its canonical twin.
    "analyst_scoring": {
        # market_regime_gate / _counter_trend_decision: composite_score bar
        # for the plain (non-against-funding) counter-trend case.
        "counter_trend_min_score": 50.0,
        # debate_gate analyst2 (confidence-vs-composite alignment): three
        # conditional branches at risk_gates L623-628.
        "analyst2_high_conf": 0.7,
        "analyst2_high_score": 40,
        "analyst2_mid_conf": 0.5,
        "analyst2_mid_score": 60,
        "analyst2_very_high_conf": 0.8,
        "analyst2_very_high_score": 20,
        # debate_gate analyst5 (whale boost): confidence floor that lets a
        # non-whale trade still earn the vote at risk_gates L645.
        "analyst5_whale_or_conf": 0.75,
    },
    # R12-C1: optional lower confidence floor for regime-aligned entries
    # (LONG in up-trend / SHORT in down-trend). None = feature off (the
    # global min_ai_confidence applies uniformly). Was implicit via
    # config.get("aligned_min_conf") in risk_gates.
    "aligned_min_conf": None,
    # 配置文件注释字段（不参与交易逻辑）
    "_comment": "",
}

# Legacy alias — code that imports DEFAULT_CONFIG gets the full canonical set.
DEFAULT_CONFIG: dict[str, Any] = CANONICAL_DEFAULTS


# ---------------------------------------------------------------------------
# R11-E1: full-config schema validation hook for the store write/read paths.
#
# F27 introduced `validate_config_updates` for *partial* patches arriving over
# the web API / CLI / `set` terminal command. That gate keeps a typed Pydantic
# whitelist in lock-step with `CANONICAL_DEFAULTS`, but it deliberately only
# inspects the keys the caller touched — leaving four dangerous back-doors:
#
#   1. `read_agent_config()` reading a hand-edited / corrupted JSON file
#      (the field was renamed, the value was quoted as a string, etc.) —
#      the bad value silently ships to every `cfg_get` consumer.
#   2. `write_agent_config(cfg)` called directly (e.g. from a script that
#      rebuilds the config from a different source) — bypasses the patch
#      gate entirely.
#   3. `restore_backup()` / `restore_snapshot()` writing a previously bad
#      config back to disk — the .bak and snapshots can store a corrupt
#      config because no gate sits between the snapshot blob and
#      `_write_raw_locked`.
#   4. `update_agent_config()` — the post-merge cfg can be valid per-patch
#      but invalid in aggregate (e.g. leverage del+leverage le independently
#      OK, but `composite_force_execute=true` set without
#      `override_requires_ai=true` slips through patch-level checks if the
#      patch set the second key as `None` for "leave alone" semantics).
#
# The functions below are the store-level safety net. They re-validate the
# *entire* candidate cfg against the canonical schema and:
#   * type / kind mismatches   -> hard error (will be raised by write paths),
#   * range violations          -> hard error,
#   * mode / FORBIDDEN_OVERRIDE -> hard error,
#   * unknown top-level keys    -> only flagged when `strict_keys=True`
#     (the legacy /api/agent/config endpoint kept lenient semantics so
#     dashboard plugins can stash their own keys; everything else uses
#     strict mode).
#
# A corrupt write is *never* a recoverable condition for a trading bot:
# better to raise and let the operator investigate than to lose a
# kill-switch silently.
# ---------------------------------------------------------------------------

# Per-key expected kind, derived from CANONICAL_DEFAULTS so it tracks the
# single source of truth.  Nested dict / list kinds use the *type* of the
# canonical default (int / float / bool / str / list / dict).  Mode is
# explicitly a 3-value enum and lives in its own per-key check below.
_TYPE_KIND_BY_KEY: dict[str, Any] = {
    key: type(default) for key, default in CANONICAL_DEFAULTS.items()
}

# Keys whose canonical default is `bool` and that must accept bool
# exclusively. Centralised so the "bool is not an int" matrix is enforced
# uniformly by both the patch-level gate and the full-cfg gate.
_STRICT_BOOL_KEYS = frozenset(
    k for k, v in CANONICAL_DEFAULTS.items() if isinstance(v, bool)
)


def _validate_cfg_value(key: str, value: Any) -> Optional[str]:
    """Return an error string if *value* fails the canonical kind check for
    *key*, otherwise None. Centralised kind/range check shared by the
    patch-level and full-cfg gates (no enum / unknown-key logic here — those
    are caller concerns).

    A *value* of ``None`` is treated as the deep-merge protocol's
    "deletion marker" and is not kind-checked — the key is popped before
    persistence, so the value never lands on disk as ``null``.  This
    matches the F27 patch gate's behaviour: ``None`` keys are stripped
    from the validation payload in :func:`_flatten_patch_for_validation`.
    """
    if value is None:
        # ``None`` is the deep-merge deletion marker — not a kind error.
        return None
    expected = _TYPE_KIND_BY_KEY.get(key)
    if expected is None:
        # Unknown key — caller decides (strict mode rejects; lenient mode
        # accepts).
        return None
    if expected is bool:
        if not isinstance(value, bool):
            return f"{key}: expected bool, got {type(value).__name__}"
        return None
    if expected is int:
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{key}: expected int, got {type(value).__name__}"
        return None
    if expected is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{key}: expected number, got {type(value).__name__}"
        return None
    if expected is str:
        if not isinstance(value, str):
            return f"{key}: expected string, got {type(value).__name__}"
        return None
    if expected is list:
        if not isinstance(value, list):
            return f"{key}: expected list, got {type(value).__name__}"
        return None
    if expected is dict:
        if not isinstance(value, dict):
            return f"{key}: expected object, got {type(value).__name__}"
        return None
    # Fallback — unknown canonical kind (e.g. tuple). Be permissive.
    return None


def _validate_critical(cfg: dict[str, Any]) -> list[str]:
    """R11-E1: run the FORBIDDEN_OVERRIDE contract against the merged view
    of *cfg*.

    Unlike :func:`validate_config_updates` (F27) which is a *patch* gate
    and inspects only the keys the caller touched, this helper sees the
    full state. The FORBIDDEN_OVERRIDE branch is the one safety check
    that *requires* a whole-view perspective: ``composite_force_execute``
    or any of the four other force-override keys can be enabled across
    two separate writes (e.g. one patch arms the lever, a second patch
    forgets to set ``override_requires_ai=true``), and the patch-level
    gate cannot catch the resulting armed state.

    We deliberately do **not** delegate to ``validate_config_updates``
    here for the mode-enum / safety-floor checks — those are designed
    for *partial* patches and would false-reject legitimate historical
    values on a whole view (the ``mode`` field was a free-form string
    before the P0-2 enum landed; older ``.bak`` files still carry the
    old value). Those checks belong in the patch gate only.
    """
    from hermes_trader.agents.config_schema import validate_forbidden_overrides
    return validate_forbidden_overrides(cfg)


def validate_config_dict(cfg: dict[str, Any], *, strict_keys: bool = True) -> list[str]:
    """Validate a *whole* config dict (post-merge) against the canonical
    schema. Returns a list of human-readable error strings (empty on pass).

    Unlike :func:`hermes_trader.agents.config_schema.validate_config_updates`
    (which only inspects the keys the caller touched), this gate checks:

    * every key in *cfg* has a kind compatible with its canonical default,
    * the merged result does not contain `composite_force_execute=true`
      (or any of the four other force-override keys) without
      `override_requires_ai=true` — a state the per-patch gate cannot catch
      if the two keys arrive in different writes,
    * the F27 range / mode-enum / safety-floor matrix is satisfied for
      the merged view.

    ``strict_keys=True`` additionally rejects unknown top-level keys
    (used by callers that want to keep the on-disk schema tight).
    ``strict_keys=False`` keeps historical legacy-endpoint semantics:
    unknown keys round-trip as-is and are not surfaced here. The two
    modes differ from ``validate_config_updates`` only in the unknowns
    (the type/range/override logic is identical, so a passing patch
    always yields a passing whole).
    """
    if not isinstance(cfg, dict):
        return [f"config: expected object, got {type(cfg).__name__}"]

    errors: list[str] = []

    # 1. Unknown-key gate.
    for key in cfg.keys():
        if key not in CANONICAL_DEFAULTS:
            if strict_keys:
                errors.append(f"unknown key: {key}")
            # Lenient: leave it for the deep-merge path to persist.

    # 2. Per-key kind check across the entire cfg.
    for key, value in cfg.items():
        # ``_comment`` is the operator's free-form note; no kind enforcement.
        if key == "_comment":
            continue
        if key not in CANONICAL_DEFAULTS:
            # Unknown keys are not type-checked in lenient mode (they
            # round-trip as-is); in strict mode they were already rejected
            # in step 1.
            continue
        if value is None:
            # Deep-merge deletion marker — _validate_cfg_value skips
            # these, skip them here too for clarity.
            continue
        err = _validate_cfg_value(key, value)
        if err is not None:
            errors.append(err)

    # 3. Delegate the F27 range / mode-enum / FORBIDDEN_OVERRIDE matrix
    # to the critical-only gate (sees the merged view, not the patch).
    # dedupe against the kind-check errors above (F27 also reports
    # "leverage: expected int" for the same key) so the operator sees
    # one error per problem, not two.
    critical_errors = _validate_critical(cfg)
    seen = set(errors)
    for ce in critical_errors:
        if ce not in seen:
            errors.append(ce)
            seen.add(ce)

    return errors


def _validate_or_raise(
    cfg: dict[str, Any], *, source: str, strict_keys: bool = True
) -> None:
    """Run :func:`validate_config_dict` on *cfg*; raise ``RuntimeError`` with
    a joined error list if any errors are found.

    *source* is a short label (e.g. ``"write_agent_config"``,
    ``"restore_snapshot"``) included in the exception message so the
    operator can see which path rejected the cfg.
    """
    errors = validate_config_dict(cfg, strict_keys=strict_keys)
    if not errors:
        return
    msg = (
        f"[config] refusing to {source} — schema validation failed "
        f"({len(errors)} error(s)): " + "; ".join(errors)
    )
    logger.error(msg)
    raise RuntimeError(msg)


def _log_validation_warnings(
    cfg: dict[str, Any], *, source: str, strict_keys: bool = True
) -> list[str]:
    """Run :func:`validate_config_dict` on *cfg* and log any errors as
    warnings.  Never raises.  Returns the list of errors (empty on pass) so
    the caller can decide whether to surface them in a metric / audit line.

    Used by :func:`read_agent_config`: a hand-edited / partially-corrupt
    config on disk must not crash the bot (CANONICAL_DEFAULTS is always the
    safety net for any key that fails), but the operator needs to see the
    problem in the logs so it can be fixed.  The deep-merge on
    CANONICAL_DEFAULTS will replace any malformed top-level value with the
    canonical default — except for keys that aren't in CANONICAL_DEFAULTS
    (those round-trip as-is and may be the operator's deliberate custom
    keys).
    """
    errors = validate_config_dict(cfg, strict_keys=strict_keys)
    if errors:
        logger.warning(
            f"[config] {source} loaded config with {len(errors)} schema "
            f"warning(s): " + "; ".join(errors)
        )
    return errors


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* into a copy of *base*.

    A value of ``None`` in *overlay* acts as a deletion marker: the
    corresponding key is removed from the result if present.
    """
    result = dict(base)
    for k, v in overlay.items():
        if v is None:
            result.pop(k, None)
        elif k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _env_override(dotted_key: str) -> Optional[str]:
    """Return the HERMES_CFG_<UPPER_KEY> env value for a dotted key, or None.

    Nested keys use double-underscore: ``dsl_exit.protect_pct`` maps to
    ``HERMES_CFG_DSL_EXIT__PROTECT_PCT``.
    """
    env_key = "HERMES_CFG_" + dotted_key.upper().replace(".", "__")
    return os.environ.get(env_key)


def _coerce(value: str, type_hint: Any) -> Any:
    """Best-effort coercion of a string env value to match *type_hint*."""
    if type_hint is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if type_hint is int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if type_hint is float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if type_hint is list:
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _lookup_default(dotted_key: str) -> Any:
    """Look up a dotted key in CANONICAL_DEFAULTS, raising KeyError if absent."""
    parts = dotted_key.split(".")
    node: Any = CANONICAL_DEFAULTS
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            raise KeyError(dotted_key)
        node = node[p]
    return node


def _lookup_in_dict(d: dict[str, Any], dotted_key: str) -> Any:
    """Look up a dotted key in an arbitrary dict, raising KeyError if absent."""
    parts = dotted_key.split(".")
    node: Any = d
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            raise KeyError(dotted_key)
        node = node[p]
    return node


def cfg_get(dotted_key: str, default: Any = None, *, config: Optional[dict[str, Any]] = None) -> Any:
    """Type-safe config lookup with env override and canonical fallback.

    Resolution order:
      1. Environment variable ``HERMES_CFG_<KEY>`` (coerced to the canonical
         default's type when possible).
      2. Value from *config* dict (or ``read_agent_config()`` if None).
      3. Canonical default from ``CANONICAL_DEFAULTS``.
      4. Caller-supplied *default* (only if the key isn't in CANONICAL_DEFAULTS).

    Usage::

        leverage = cfg_get("leverage")                    # -> 12
        protect  = cfg_get("dsl_exit.protect_pct")        # -> 1.25
        custom   = cfg_get("nonexistent", 42)             # -> 42
    """
    # 1. Environment override
    env_val = _env_override(dotted_key)
    if env_val is not None:
        try:
            type_hint = type(_lookup_default(dotted_key))
        except KeyError:
            type_hint = str
        return _coerce(env_val, type_hint)

    # 2. Config file value
    if config is None:
        config = read_agent_config()
    try:
        return _lookup_in_dict(config, dotted_key)
    except KeyError:
        pass

    # 3. Canonical default
    try:
        return _lookup_default(dotted_key)
    except KeyError:
        pass

    # 4. Caller default
    return default


def apply_coin_override(config: dict[str, Any], coin: Optional[str]) -> dict[str, Any]:
    """Return a copy of *config* with ``coin_overrides[coin]`` deep-merged on top.

    This is the single chokepoint for per-coin parameter isolation. Call it at
    the start of a per-coin code path (e.g. executor.maybe_execute) and every
    downstream consumer — risk gates, order sizing, DSL exit policy — reads the
    merged view transparently through ``config.get(...)`` / ``cfg_get(...,
    config=config)``.

    An override of ``{"enabled": false}`` is exposed as ``config["enabled"]``
    (separate from the global ``mode``) so callers can reject a disabled coin
    without touching the global mode. Other keys (leverage, dsl_exit,
    max_trade_notional_usd, ...) override the matching global values.

    Returns *config* unchanged when *coin* is falsy or has no override.
    """
    if not coin:
        return config
    overrides = (config.get("coin_overrides") or {}).get(coin)
    if not overrides or not isinstance(overrides, dict):
        return config
    # Strip the override map itself before merging so a per-coin override
    # cannot accidentally replace the whole map.
    base = {k: v for k, v in config.items() if k != "coin_overrides"}
    return _deep_merge(base, overrides)


def read_agent_config() -> dict[str, Any]:
    """Read the agent config from .agent-config.json.

    The returned dict is merged on top of CANONICAL_DEFAULTS so that newly
    added keys are always present even if the on-disk config predates them.

    A corrupted file is NOT silently swallowed as the old `except
    (json.JSONDecodeError, OSError): return DEFAULT_CONFIG` did — that masked
    disk/permissions failures as "mode=OFF" and, worse, a subsequent
    write_agent_config would overwrite the (still-recoverable) corrupt file
    with the caller's view. A shared lock guards against torn reads while a
    writer holds the exclusive lock.
    """
    raw = _read_raw_config()
    if raw is None:
        return dict(CANONICAL_DEFAULTS)
    # R11-E1: log any schema violations found in the on-disk file but do
    # NOT raise.  A hand-edited / partially-corrupt config must not crash
    # the bot — the deep-merge on CANONICAL_DEFAULTS will overwrite any
    # malformed top-level value with the canonical default (for keys
    # _in_ CANONICAL_DEFAULTS) and the per-coin path will use those
    # defaults transparently.  Unknown / not-in-canonical keys round-trip
    # as-is so the operator's deliberate custom keys are preserved.
    # strict_keys=False to preserve the historical "raw disk file is
    # lenient" semantics: a key the operator added (e.g. for a dashboard
    # plugin) is not an error.
    _log_validation_warnings(raw, source="read_agent_config", strict_keys=False)
    merged = _deep_merge(CANONICAL_DEFAULTS, raw)
    # R12-C1: a null in the on-disk file is a deep-merge *deletion marker*.
    # For canonical keys whose default is itself None (feature-off sentinel,
    # e.g. aligned_min_conf), a full merged view persisted by
    # update_agent_config (then re-read) would silently drop the key from the
    # merged/dumped view — the runtime behavior is unchanged (dict.get still
    # yields None) but audit visibility is lost. Re-materialize such keys so
    # they stay visible in dashboard dumps and `set`-able. Non-None keys are
    # deliberately NOT backfilled here (their absence never follows from a
    # canonical null).
    for key, default in CANONICAL_DEFAULTS.items():
        if default is None and key not in merged:
            merged[key] = None
    return merged


def _read_raw_config() -> Optional[dict[str, Any]]:
    """Read and parse the raw JSON config under a shared flock, or None on
    any failure (see :func:`_read_raw_locked` for semantics).

    F20: thin flock wrapper around :func:`_read_raw_locked` so the same read
    body can run inside the exclusive lock held by :func:`update_agent_config`
    without re-opening the lock file (flock binds to the open file
    description — a second fd in the *same* process asking for LOCK_EX while
    this one holds it would self-deadlock).
    """
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        return _read_raw_locked()
    except OSError as e:
        logger.error(
            f"[config] cannot read {CONFIG_PATH}: {e} — falling back to CANONICAL_DEFAULTS"
        )
        return None
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def _read_raw_locked() -> Optional[dict[str, Any]]:
    """Read and parse the raw JSON config, assuming the caller already holds
    a shared or exclusive flock on ``_CONFIG_LOCK_PATH``.

    P1-10: the parsed dict is cached keyed by (mtime_ns, size). A cheap
    ``stat()`` decides whether the open + json.load path is needed. The
    cached object is never handed out directly: callers (via
    ``_deep_merge``) may hold/mutate nested leaves, so every return — hit
    or miss — is a ``deepcopy`` of a pristine copy.
    """
    global _RAW_CACHE, _RAW_CACHE_SIG
    sig = _config_sig()
    if sig is not None:
        with _RAW_CACHE_LOCK:
            if _RAW_CACHE is not None and sig == _RAW_CACHE_SIG:
                return deepcopy(_RAW_CACHE)
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            logger.error(
                f"[config] {CONFIG_PATH} top-level is {type(cfg).__name__}, "
                f"expected object — falling back to CANONICAL_DEFAULTS"
            )
            _invalidate_raw_cache()
            return None
        # Re-stat under the lock so the signature matches the bytes parsed.
        sig = _config_sig()
        if sig is not None:
            with _RAW_CACHE_LOCK:
                _RAW_CACHE = deepcopy(cfg)
                _RAW_CACHE_SIG = sig
        return deepcopy(cfg)
    except FileNotFoundError:
        logger.warning(f"[config] {CONFIG_PATH} not found — using CANONICAL_DEFAULTS")
        _invalidate_raw_cache()
        return None
    except json.JSONDecodeError as e:
        logger.error(
            f"[config] {CONFIG_PATH} is CORRUPT (JSON error at line {e.lineno} col "
            f"{e.colno}): {e.msg}. Falling back to CANONICAL_DEFAULTS — "
            f"investigate before trading; do NOT overwrite the file blindly."
        )
        return None


def write_agent_config(cfg: dict[str, Any], *, backup: bool = True) -> None:
    """Write the agent config to .agent-config.json (atomic replace + lock).

    F20: thin flock wrapper around :func:`_write_raw_locked` so the write
    body can run inside the exclusive lock already held by
    :func:`update_agent_config` (a second LOCK_EX fd in the same process
    would self-deadlock — flock binds to the open file description).
    """
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # R11-E1: validate BEFORE touching the disk. The flock is held so
        # this is the only place validation can run for a direct
        # write_agent_config() call. Raises RuntimeError on critical
        # schema violation (wrong type / out-of-range / mode typo /
        # FORBIDDEN_OVERRIDE armed) — the .bak and .tmp files are
        # untouched. Unknown keys are still accepted (strict_keys=False)
        # to preserve the historical "raw disk file is lenient"
        # semantics — a dashboard plugin that stashed its own keys
        # before R11-E1 must keep working.
        _validate_or_raise(cfg, source="write_agent_config", strict_keys=False)
        _write_raw_locked(cfg, backup=backup)
    except OSError as e:
        logger.error(f"[config] FAILED to write {CONFIG_PATH}: {e}")
        raise
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def _write_raw_locked(cfg: dict[str, Any], *, backup: bool = True) -> None:
    """Write *cfg* to disk, assuming the caller already holds LOCK_EX on
    ``_CONFIG_LOCK_PATH``. See :func:`write_agent_config` for semantics.

    When *backup* is True (default), the previous config is copied to
    ``.agent-config.json.bak`` before overwriting, enabling rollback.

    Under a Docker single-file bind mount, os.replace() onto the mounted
    target fails with EBUSY ("Device or resource busy") because the kernel
    cannot swap the inode a mount point points at. In that case we fall back
    to truncating and rewriting the mounted file in place while still holding
    the exclusive flock, so concurrent readers see either the old or new
    contents rather than a torn file.
    """
    # Backup the current config before overwriting
    if backup:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r") as src:
                    old_data = src.read()
                with open(_BACKUP_PATH, "w") as dst:
                    dst.write(old_data)
                    dst.flush()
                    os.fsync(dst.fileno())
        except OSError as e:
            logger.warning(f"[config] backup failed (non-fatal): {e}")

    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except OSError as replace_err:
        # EBUSY: single-file bind mount (e.g. docker -v host:guest).
        # EXDEV/EPERM can surface on similarly restricted filesystems.
        # Fall back to an in-place overwrite of the mounted target.
        if replace_err.errno not in (getattr(os, "EBUSY", 16),):
            raise
        logger.warning(
            f"[config] os.replace onto {CONFIG_PATH} hit EBUSY "
            f"(bind-mounted file); rewriting in place instead"
        )
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.remove(tmp)
        except OSError:
            pass
    # P1-10: drop any cached raw config so the next read reloads from
    # disk. Covers both the os.replace() and EBUSY in-place paths.
    _invalidate_raw_cache()
    logger.info(f"[config] written {len(cfg)} keys to {CONFIG_PATH}")


@contextmanager
def update_agent_config(*, backup: bool = True) -> Iterator[dict[str, Any]]:
    """F20: cross-process read-modify-write critical section for the agent
    config.

    Opens the flock file once and takes LOCK_EX for the whole RMW: reads the
    current effective config (canonical defaults deep-merged over the raw
    file), yields it for in-place mutation, then — if the body exits cleanly
    — writes it back under the *same* lock. ``threading.Lock`` cannot
    serialize a CLI/daemon process against the web process; flock can, so
    every read-then-write config path (dashboard handler, the legacy
    ``POST /api/agent/config`` endpoint, the ``config`` CLI) must go through
    this context manager instead of calling read_agent_config() /
    write_agent_config() separately.

    Aborts (writes nothing) when:
      * the body raises — the exception propagates, the on-disk file is
        untouched;
      * the on-disk config is missing, unreadable, or corrupt — the same
        None the plain read path treats as "fall back to defaults". Writing
        a defaults-blob here would silently clobber a corrupt file operators
        are explicitly told to investigate, so raise instead.

    Do NOT call read_agent_config()/write_agent_config() inside the body:
    those open their own fd and flock self-deadlocks within one process.
    """
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        raw = _read_raw_locked()
        if raw is None:
            raise RuntimeError(
                f"[config] {CONFIG_PATH} is missing or corrupt — refusing to "
                f"overwrite blindly; investigate and restore from .bak first"
            )
        cfg = _deep_merge(CANONICAL_DEFAULTS, raw)
        yield cfg
        # R11-E1: the body mutated cfg; validate the *post-merge* state
        # before persisting.  This catches aggregated violations the
        # patch-level gate cannot — most importantly FORBIDDEN_OVERRIDE
        # where one write set `composite_force_execute=true` and a
        # later write toggled `override_requires_ai` away, producing
        # an armed state that per-patch validation never saw as
        # simultaneous.  Unknown keys are accepted (strict_keys=False)
        # to preserve the historical round-trip contract.
        _validate_or_raise(cfg, source="update_agent_config", strict_keys=False)
        _write_raw_locked(cfg, backup=backup)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def backup_config() -> Optional[dict[str, Any]]:
    """Read and return the last backup config, or None if unavailable."""
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        if not os.path.exists(_BACKUP_PATH):
            return None
        with open(_BACKUP_PATH, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def restore_backup() -> bool:
    """Restore the config from the last backup. Returns True on success.

    R11-E1: delegates to :func:`write_agent_config`, so the schema gate
    runs automatically. A bad .bak (e.g. hand-edited and never validated)
    raises ``RuntimeError`` instead of being silently restored.
    """
    old = backup_config()
    if old is None:
        return False
    try:
        write_agent_config(old, backup=False)
    except RuntimeError as e:
        # The .bak is corrupt — surface a clear log line distinct from
        # the generic write rejection so the operator can tell which
        # recovery path failed.
        logger.error(
            f"[config] refusing to restore from backup {_BACKUP_PATH}: {e}"
        )
        return False
    logger.warning(f"[config] restored from backup {_BACKUP_PATH}")
    return True


# ---------------------------------------------------------------------------
# Multi-version manual snapshots.
#
# The single rolling ``.bak`` is overwritten on every write and therefore only
# lets an operator undo the *most recent* change. Manual snapshots created via
# the dashboard ("手动备份") live alongside it as
# ``.agent-config.json.snap.<unix_ts>.json`` and are never touched by normal
# writes, so they provide named recovery points spanning many changes.
# ---------------------------------------------------------------------------
_SNAP_GLOB = "*.snap.*.json"
_SNAP_PREFIX = CONFIG_PATH + ".snap."
_SNAP_SUFFIX = ".json"
_MAX_SNAPSHOTS = 20


def _snap_path(ts: int) -> str:
    return f"{_SNAP_PREFIX}{ts}{_SNAP_SUFFIX}"


def create_snapshot(reason: str = "manual") -> dict[str, Any]:
    """Copy the current config to an immutable timestamped snapshot.

    Returns metadata ``{"id", "ts", "reason", "keys", "size"}``. Raises
    ``OSError`` if the current config cannot be read.
    """
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with open(CONFIG_PATH, "r") as f:
            data = f.read()
        # Validate before snapshotting so we never archive a corrupt file.
        cfg = json.loads(data)
        ts = int(time.time())
        path = _snap_path(ts)
        # Collision guard (clock skew / double-click) — bump the second.
        while os.path.exists(path):
            ts += 1
            path = _snap_path(ts)
        with open(path, "w") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        logger.info(f"[config] snapshot saved -> {path} ({reason})")
        _prune_snapshots_nolock()
        return {
            "id": f"snap-{ts}",
            "ts": ts,
            "reason": reason,
            "keys": len(cfg) if isinstance(cfg, dict) else 0,
            "size": len(data),
        }
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def list_snapshots() -> list[dict[str, Any]]:
    """Return all manual snapshots, newest first. Each item has id/ts/reason."""
    import glob
    snaps: list[dict[str, Any]] = []
    snap_basename_prefix = os.path.basename(_SNAP_PREFIX)
    for path in glob.glob(_SNAP_PREFIX + "*" + _SNAP_SUFFIX):
        fname = os.path.basename(path)
        # <config>.snap.<ts>.json
        try:
            ts_str = fname[len(snap_basename_prefix):-len(_SNAP_SUFFIX)]
            ts = int(ts_str)
        except (ValueError, IndexError):
            continue
        snaps.append({"id": f"snap-{ts}", "ts": ts, "reason": "manual"})
    snaps.sort(key=lambda s: s["ts"], reverse=True)
    return snaps


def restore_snapshot(ts: int) -> bool:
    """Restore a specific manual snapshot by unix timestamp. Returns True.

    Mirrors restore_backup(): do NOT hold the config flock here. flock locks
    are bound to the open file description, so a second fd in this same
    process (opened by write_agent_config) blocking on LOCK_EX would
    self-deadlock. write_agent_config takes the lock itself.

    R11-E1: delegates to :func:`write_agent_config`, so the schema gate
    runs automatically. A snapshot that was created from a bad cfg (e.g.
    taken before the R11-E1 gate existed) raises ``RuntimeError`` instead
    of being silently restored.
    """
    path = _snap_path(ts)
    try:
        if not os.path.exists(path):
            return False
        with open(path, "r") as f:
            old = json.load(f)
        # Do not overwrite the rolling .bak with the snapshot itself; a
        # restore is a recovery action, not a normal edit.
        try:
            write_agent_config(old, backup=False)
        except RuntimeError as e:
            logger.error(
                f"[config] refusing to restore from snapshot {path}: {e}"
            )
            return False
        logger.warning(f"[config] restored from snapshot {path}")
        return True
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[config] snapshot restore failed: {e}")
        return False


def _prune_snapshots_nolock() -> None:
    """Keep only the newest _MAX_SNAPSHOTS; delete the rest. Lock held by caller."""
    import glob
    paths = glob.glob(_SNAP_PREFIX + "*" + _SNAP_SUFFIX)
    if len(paths) <= _MAX_SNAPSHOTS:
        return
    # Sort by mtime ascending, delete the oldest excess.
    paths.sort(key=lambda p: os.path.getmtime(p))
    for old in paths[: len(paths) - _MAX_SNAPSHOTS]:
        try:
            os.remove(old)
            logger.info(f"[config] pruned old snapshot {old}")
        except OSError:
            pass
