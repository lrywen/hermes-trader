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

from hermes_trader.agents import atomic_io

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
    # F4 (supplemental audit 2026-08-31): keys that config_schema declares as
    # float must have a float canonical default too — _TYPE_KIND_BY_KEY derives
    # the kind from type(default), so an int literal made the store-side gate
    # expect int while the on-disk/pydantic value is float, spamming
    # "expected int, got float" schema warnings on every config read. The float
    # branch accepts both int and float; the literals just must be float-typed.
    "max_trade_notional_usd": 800.0,
    "tp_scale_fraction": 0.5,
    "max_concurrent": 10,
    "max_total_notional_pct": 10.0,
    "max_daily_loss_usd": -30.0,  # F4: float default aligns with schema (supplemental audit 2026-08-31)
    # B-M11 (deep audit 2026-08-28): the global-halt and per-coin circuit
    # breakers only block NEW entries — positions already open keep bleeding
    # to their DSL stops during the halt window. These switches make them
    # HARD: when armed, the trading loop market-closes every open position
    # (global halt) / the halted coin's position (coin circuit) the moment
    # the breaker trips. H-1 (audit 2026-08-29): flipped to DEFAULT ON — a
    # tripped breaker means risk is already out of control and leaving a
    # 10-U micro-book naked (relying solely on each coin's own resting stop)
    # is the more dangerous contract. Operators who want the old "block
    # entries only" behavior can set either key to false explicitly.
    "auto_flatten_on_global_halt": True,
    "auto_flatten_on_coin_circuit": True,
    # C3 (HYPE RCA 2026-08-21 item 5): blow-up-level self-halt. When a SINGLE
    # closing trade realizes a leveraged ROE loss at/under `roe_halt_threshold_pct`
    # (default -50%, i.e. half the margin gone), flip the bot to mode=OFF and
    # fire a risk alert. This is the nuclear kill-switch that the tiered breakers
    # (time-windowed, open-blocking only) and the daily-loss USD switch do not
    # cover: a single catastrophic gap-through (HYPE: -252% ROE). Default OFF —
    # switching OFF is a deliberate operator action; the alert/audit still help.
    "roe_halt_enabled": False,
    "roe_halt_threshold_pct": -50.0,
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
    "min_market_volume_usd": 5_000_000.0,  # F4: float per schema (supplemental audit 2026-08-31)
    "min_hip3_volume_usd": 5_000_000.0,    # F4: float per schema (supplemental audit 2026-08-31)
    "min_short_volume_usd": 50_000_000.0,  # F4: float per schema (supplemental audit 2026-08-31)
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
        # A-F5 (deep audit 2026-08-28): breach_confirm_sec default 0.0 → 4.0
        # (audit: 3–5s). A single instantaneous mid tick through the floor no
        # longer closes; the breach must persist 4s AND the oracle index price
        # must confirm it (dsl_exit.get_index_prices).
        "consecutive_breaches_required": 1,
        "breach_confirm_sec": 4.0,
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
    # O-3 (P1 audit): the TA sidestep force-execute switch bypasses AI
    # confirmation, so like every other force_*/bypass switch its canonical
    # default is False — arming it is an explicit operator decision (and is
    # caught by the FORBIDDEN_OVERRIDE config gate).
    "ta_sidestep_force_execute": False,
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
        # R13-B5: fast-EMA slope lookback window (bars) for the trend
        # classifier (market_regime._SLOPE_LOOKBACK) and the per-proxy regime
        # cache freshness TTL (market_regime.REGIME_TTL_S, 5 min). Was
        # module-literal / function-local — now visible + tunable.
        "slope_lookback": 8,
        "ttl_sec": 300,
    },
    # R13-B5: 5-component continuous trend-strength score (byte-aligned with
    # scripts/backtest_ab_compare._regime_score). Consumed by
    # market_regime.regime_strength_score() AND executor.regime_strength_label()
    # (single source via market_regime.regime_score_params()); before R13-B5
    # the weights/calibration were two byte-copied literal sets (one per file)
    # invisible to cfg. Defaults are the exact calibrated literals.
    "regime_score": {
        # component weights (sum == 1.0)
        "weight_adx": 0.25,
        "weight_atr": 0.225,
        "weight_ema_align": 0.175,
        "weight_price_ext": 0.175,
        "weight_obv": 0.175,
        # calibration anchors: ADX 15 -> 0, 45 -> 1 (full span 30)
        "adx_zero": 15.0,
        "adx_full_span": 30.0,
        # ATR% 0.2% -> 0, 1.0% -> 1 (full span 0.8)
        "atr_pct_zero": 0.2,
        "atr_pct_full_span": 0.8,
        # |EMA8-EMA21| gap% reaching 0.5% -> 1.0
        "ema_gap_full_pct": 0.5,
        # distance from EMA21 reaching 2.0 ATR -> 1.0
        "price_ext_full_atr": 2.0,
        # OBV flat (no slope) partial credit (aligned=1.0, opposing=0.0)
        "obv_flat_score": 0.3,
        # score indicator periods (distinct from classifier EMA20/30)
        "ema_fast": 8,
        "ema_slow": 21,
        "ind_period": 14,
        "min_candles": 50,
        "obv_slope_period": 10,
    },
    # R13-B6: funding-crowding regime classifier (hyperfeed.py). Consumed by
    # hyperfeed._compute_funding_regime() / market_get_funding_regime() and
    # read on the risk-gate hot path (risk_gates._funding_regime_for). The
    # 5-min cache TTL was the twin of regime_classifier.ttl_sec (R13-B5) —
    # one was wired, the other still a module literal. Defaults are the exact
    # hyperfeed literals: ±0.0001 funding crowding bar, OI floors 1e7 (crypto)
    # / 1e6 (HIP-3 equity+commodity), per-class long-vs-short count margin 5.
    "funding_regime": {
        "ttl_sec": 300,
        "crowded_funding_threshold": 0.0001,
        "oi_floor_crypto": 10000000.0,
        "oi_floor_other": 1000000.0,
        "class_dominance_margin": 5,
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
    "research_rescore_delta": 0.0,  # F4: float per schema (supplemental audit 2026-08-31)
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
    # R13-B7: free-signal-suite tunables. Each block mirrors the literals that
    # were hardcoded in the corresponding module; the modules keep their module
    # constants as fallback symbols but resolve every leaf through cfg_get so
    # env (HERMES_CFG_<BLOCK>__<KEY>) and dashboard overrides actually reach
    # the hot path (direct dict reads silently ignored env — the gex caution
    # drift fixed here: canonical said 10.0 while three fallbacks said 1.0).
    "options_gex": {
        "ttl_sec": 900,              # CBOE delayed feed; 15-min structural cache
        "http_timeout_s": 12.0,      # per-request CBOE fetch bound
    },
    "short_volume": {
        "ttl_sec": 3600,             # FINRA file is daily; an hour is plenty
        "http_timeout_s": 12.0,      # per-day FINRA fetch bound
        "crowded_ratio": 0.60,       # >= → squeeze fuel
        "light_ratio": 0.35,         # <= → little short pressure
        "trend_delta": 0.03,         # series first-vs-last move for rising/falling
        "lookback_days": 5,          # trading days walked back per scan
    },
    "crypto_whale": {
        "ttl_sec": 120,              # Binance aggTrades rolling window cache
        "http_timeout_s": 2.5,       # per-page bound (6 sequential pages max)
        "cache_max": 1024,           # per-process cache entry cap
        "window_minutes": 15,        # rolling aggTrades window
        "min_usd": 100000,           # print >= this counts as a whale print
        "bias_threshold": 0.20,      # |net|/whale $ >= this for a directional bias
        "max_pages": 6,              # pagination cap on the window walk
    },
    "news_catalyst": {
        "ttl_sec": 300,              # GDELT/RSS cache; news moves fast
        "http_timeout_s": 3.0,       # per-request bound (2 parallel GDELT calls)
        "surge_breaking_x": 2.5,     # latest coverage bin >= 2.5x baseline = breaking
        "surge_elevated_x": 1.5,     # >= 1.5x baseline = elevated coverage
        "timespan": "1h",            # GDELT query timespan
        "max_records": 30,           # ArtList maxrecords / headline cap
        "rss_limit": 25,             # rss_headlines headline cap
        "fetch_max_workers": 2,      # parallel GDELT ArtList+TimelineVol pool
    },
    "whale_index": {
        "min_volume_usd": 1000000,       # smart_money_concentration 24h-vol floor
        "funding_confidence_scale": 0.0001,  # |funding|/this = concentration confidence
        "oi_vol_ratio_min": 10,          # OI/($M vol) above this = high-OI flag
        "oi_vol_confidence_norm": 50,    # ratio/this = high-OI confidence
        "min_oi_usd": 5000000,           # OI notional floor for anomaly/surge
        "max_funding_threshold": -0.00001,  # funding must be below this
        "funding_norm": 0.00008,         # |funding| mapping to ~full confidence
        "flat_price_pct": 10,            # |24h price move| below this = flat
        "min_oi_growth_pct": 8.0,        # OI surge since last snapshot
        "max_price_move_pct": 4.0,       # price-still-flat gate for surge
        "surge_norm_pct": 25.0,          # OI growth mapping to ~full confidence
        "min_confidence": 0.05,          # whale_accumulation_map confidence floor
        "mcp_min_confidence": 0.1,       # get_whale_signals (MCP) confidence floor
        "mcp_top_n": 10,                 # get_whale_signals result cap
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
    # R13-B11: memory equity quality-gate knobs (memory.py). Seven leaves:
    # the track_daily_pnl partial-dex degraded-read filter (implausible move
    # fraction, immediate-accept crash fraction, the same-tick filter window,
    # and the re-confirm streak), the avg_exit_slip_bps lookback window in
    # days and its minimum-sample bar, and the non-forced flush throttle in
    # seconds. They were previously a bare `_IMPLAUSIBLE_PCT = 0.25` literal
    # and `< 180` / `streak < 2` literals in track_daily_pnl, the
    # `days=30.0` / `min_samples=3` signature defaults of avg_exit_slip_bps,
    # and module-load os.environ.get reads
    # (HERMES_EQUITY_CRASH_DOWN_PCT / HERMES_MEMORY_FLUSH_THROTTLE_S) — never
    # in CANONICAL_DEFAULTS. memory._memory_quality_params() keeps the two
    # legacy env vars as the top-priority override, then falls through to
    # this block via cfg_get (HERMES_CFG_MEMORY_QUALITY__* env +
    # agent-config); the literals remain as the final fallback. Defaults
    # mirror the memory.py literals verbatim; behaviour unchanged.
    "memory_quality": {
        "implausible_pct": 0.25,
        "crash_down_pct": 0.40,
        "filter_window_sec": 180,
        "reconfirm_streak": 2,
        "slip_window_days": 30.0,
        "slip_min_samples": 3,
        "flush_throttle_s": 0.2,
    },
    # R13-B11: dashboard equity read-side quality-gate knobs (dashboard.py).
    # Four leaves: the equity-curve dip flag ratio and trailing reference
    # window (_equity_curve_payload partial-dex degraded-point flagging), the
    # summary heartbeat staleness threshold in seconds (_summary_payload
    # "scanning"/"stale" status), and the closed-trades cross-source
    # de-duplication window in ms (_closed_trades_payload). They were
    # previously module-load os.environ.get reads
    # (HERMES_EQUITY_DIP_RATIO / HERMES_EQUITY_DIP_WINDOW /
    # HERMES_CLOSED_TRADES_DEDUP_MS) and a bare `> 180` literal — never in
    # CANONICAL_DEFAULTS. dashboard._dashboard_equity_params() keeps every
    # legacy env var as the top-priority override, then falls through to this
    # block via cfg_get (HERMES_CFG_DASHBOARD_EQUITY__* env + agent-config);
    # the module-level _EQUITY_DIP_RATIO / _EQUITY_DIP_WINDOW attributes stay
    # (tests monkeypatch.setattr them) and are read live as the literal
    # fallback layer. Defaults mirror the dashboard.py literals verbatim;
    # behaviour unchanged.
    "dashboard_equity": {
        "dip_ratio": 0.7,
        "dip_window": 15,
        "stale_tick_age_s": 180,
        "dedup_window_ms": 5000,
    },
    # R13-B12: HTTP-edge cache TTLs (dashboard.py / public.py / server.py).
    # Five keys cover the three public JSON poll endpoints, the per-coin
    # research verdict cache, and the TTL-cache singleflight waiter timeout.
    # Legacy HERMES_SUMMARY_TTL_S / HERMES_EQUITY_CURVE_TTL_S /
    # HERMES_CLOSED_TRADES_TTL_S / HERMES_RESEARCH_HTTP_CACHE_S env vars
    # remain the top-priority channel; ttl_load_wait_s was a bare literal.
    "http_cache": {
        "summary_ttl_s": 2.0,
        "equity_curve_ttl_s": 30.0,
        "closed_trades_ttl_s": 10.0,
        "research_cache_ttl_s": 30.0,
        "ttl_load_wait_s": 60.0,
    },
    # R13-B13: Hyperliquid client-layer knobs (client/exchange.py /
    # hl_client.py / ws_client.py). Twelve leaves cover the SDK HTTP timeout,
    # the cross-margin fallback leverage, the IOC slippage caps, the meta /
    # ATR / candle / funding cache TTLs+sizes, and the WebSocket staleness /
    # heartbeat / sequence tolerances. Legacy HERMES_HL_SDK_TIMEOUT_S /
    # HERMES_DEFAULT_LEVERAGE / HERMES_MAX_SLIPPAGE_PCT /
    # HERMES_MAX_SLIPPAGE_CLOSE_PCT / HERMES_META_TTL_S / HERMES_ATR_TTL_S /
    # HERMES_CANDLE_CACHE_TTL_S / HERMES_CANDLE_CACHE_MAX /
    # HERMES_FUNDING_CACHE_TTL_S / HERMES_WS_MAX_STALE_SECONDS /
    # HERMES_WS_HEARTBEAT_S / HERMES_WS_SEQ_MAX_BACKWARD env vars remain the
    # top-priority channel. default_leverage (5) is the cross-margin fallback
    # only — NOT the top-level trading `leverage` (10); the two never merged.
    "hl_client_io": {
        "sdk_timeout_s": 30.0,
        "default_leverage": 5,
        "max_slippage_pct": 1.5,
        "max_slippage_close_pct": 5.0,
        "meta_ttl_s": 3600.0,
        "atr_ttl_s": 60.0,
        "candle_cache_ttl_s": 90.0,
        "candle_cache_max": 512,
        "funding_cache_ttl_s": 300.0,
        "ws_max_stale_s": 30,
        "ws_heartbeat_s": 10.0,
        "ws_seq_max_backward": 1024,
        "ws_max_tick_jump_frac": 0.25,
    },
    # R13-B13: Hyperliquid rate-limiter knobs (client/rate_limit.py +
    # hl_client.py call sites). Seven leaves cover token-bucket refill /
    # capacity, the trading-path max budget wait, the 429 retry count, the
    # opportunistic (observability) budget wait, and the two gate switches
    # (cross-process shared bucket; in-process per-endpoint serialization).
    # Legacy HERMES_HL_RATE_REFILL_PER_SEC / HERMES_HL_RATE_CAPACITY /
    # HERMES_HL_RATE_MAX_WAIT_S / HERMES_HL_429_RETRIES /
    # HERMES_HL_RATE_OPPORTUNISTIC_WAIT_S / HERMES_HL_RATE_SHARED /
    # HERMES_HL_RATE_PER_ENDPOINT_GATE env vars remain the top-priority
    # channel (the gate switch keeps its historical call-time env read so a
    # post-import toggle still takes effect). The shared-bucket state FILE
    # path stays an env-only deployment knob.
    "hl_rate_limit": {
        "rate_refill_per_sec": 20.0,
        "rate_capacity": 600,
        "rate_max_wait_s": 30.0,
        "rate_429_retries": 2,
        "rate_opportunistic_wait_s": 2.0,
        "rate_shared": True,
        "rate_per_endpoint_gate": True,
    },
    # P2-3: bps the exchange backup stop sits behind the DSL floor (executor
    # SL ratchet coordination); and the funding-rate history lookback window
    # in hours (research display / against-funding context).
    "sl_buffer_bps": 10.0,
    # H4 (deep audit 2026-08-29): assumed maintenance-margin rate (PERCENT)
    # for the pre-trade liquidation-price estimate. The gate requires
    # liq_distance_pct (= 100/leverage - this rate) > stop_distance_pct +
    # sl_buffer. HL's actual maintenance margin is tier-dependent; 1.0% is a
    # conservative flat assumption for small perps. Set to 0 to disable.
    "liquidation_maint_margin_pct": 1.0,
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
        # B-F2 (deep audit 2026-08-28): consecutive losing closes on one coin
        # before new entries on that coin are blocked. The streak was recorded
        # in memory (record_loss_outcome) but no gate ever read it. <=0 disables.
        "consecutive_loss_limit": 3,
        # B-F6: per-coin CUMULATIVE daily realized loss, as % of start-of-day
        # equity. Backs the per_coin_daily_loss_gate; complements the per-trade
        # single_coin_loss_pct (one large spot loss) with the many-small-loss
        # accumulation case. <=0 disables.
        "coin_daily_loss_pct": 5.0,
        # B-F7: account-wide max drawdown from the all-time equity high-water
        # mark, as a %. Blocks ALL new entries beyond it (a kill-switch the
        # daily-loss gate cannot cover: a slow multi-day grind down trips no
        # single day's limit but still blows the account). <=0 disables.
        "max_drawdown_pct": 15.0,
        # H6/C-M3 (deep audit 2026-08-28): after an order placement whose
        # response was LOST (408/read-timeout/SSL drop), the executor polls
        # userFills to decide filled vs not-filled. When the exchange itself
        # is unreachable the outcome stays unknown (a possible orphan); N
        # consecutive unresolvable outcomes trigger a global auto-entry halt
        # for halt_min minutes so we stop spraying orders at a deaf exchange.
        # halt_n <= 0 disables the halt (rehydrate still runs each time).
        "resp_unknown_halt_n": 3,
        "resp_unknown_halt_min": 60.0,
    },
    # ta_late_entry (deep audit 高危项, 2026-08-30): late-entry hard gate.
    # The same late_entry_check() pure function (agents/ta_filter.py) runs in
    # three places with ONE source of truth for thresholds:
    #   1. analyze_perception() — pre-filter before the paid LLM debate
    #   2. ta_late_entry_gate() — hard pre-trade gate in eval_all_gates()
    #   3. scripts/backtest.py — backtest entry path (rule parity)
    # mode controls the pre-trade GATE only (the pre-filter veto is always
    # active): "off" = gate absent; "shadow" = verdict recorded + metrics but
    # never blocks (gray-release, run 3-7 days); "enforce" = blocks orders.
    # Thresholds: 4h RSI / extension-in-ATR veto is OR semantics; when 4h ADX
    # >= adx_trend_threshold and the EMA trend aligns with the trade side the
    # relaxed limits apply (trend exception); on a 4h veto with 15m RSI not
    # yet extreme the trade passes (multi-timeframe continuation override).
    # Per-trader tuning is via env HERMES_CFG_TA_LATE_ENTRY__<KEY>; no global
    # hard-coded numbers outside this block.
    "ta_late_entry": {
        "mode": "shadow",
        # --- 4h hard veto thresholds (normal regime) ---
        "rsi_ob": 75,
        "rsi_os": 25,
        "ext_ob": 2.5,
        "ext_os": -2.5,
        # --- trend exception: relax limits in a strong aligned trend ---
        "trend_relax_enabled": True,
        "adx_trend_threshold": 35,
        "rsi_ob_relaxed": 82,
        "rsi_os_relaxed": 18,
        "ext_ob_relaxed": 3.5,
        "ext_os_relaxed": -3.5,
        # --- multi-timeframe: 15m RSI continuation override ---
        # Phase 0 (deep audit R3, 2026-08-30): DEFAULT OFF. The 15m fetch is
        # the only cold candle HTTP in the gate path (the screen never warms
        # that key), and a small frame "veto of the veto" inverts the gate's
        # HTF-tail-filter semantics. Opt back in per-trader via env once
        # shadow evidence supports it.
        "mtf_enabled": False,
        "rsi15m_ob": 72,
        "rsi15m_os": 28,
        # --- data requirements / fetch sizing ---
        "min_bars_4h": 30,
        "min_bars_15m": 20,
        "fetch_bars": 100,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
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
    # R13-B8: trigger composite-score weights (TRIGGER_CONFIG["weights"],
    # perception.py L316/323 -> triggers.composite_score). The 12 weights
    # previously lived only in the module-level TRIGGER_CONFIG dict and had no
    # env / dashboard / set-channel — an operator could not tune or even read
    # them at runtime. Leaf names are snake_case (canonical convention);
    # config.py.trigger_weights_params() maps them to the camelCase runtime
    # keys that composite_score indexes by trigger name. Six weights are
    # intentionally 0.0 (net-negative / surfacing-only triggers), hence the
    # >= 0 guard. Defaults mirror TRIGGER_CONFIG verbatim; behaviour unchanged.
    "trigger_weights": {
        "trend_strength": 0.55,
        "pct_move_spike": 0.40,
        "breakout": 0.30,
        "volume_spike": 0.25,
        "momentum_burst": 0.20,
        "volume_buildup_1h": 0.15,
        "higher_lows_1h": 0.0,
        "trend_flip_1h": 0.0,
        "range_compression": 0.0,
        "uptrend_momentum": 0.0,
        "downtrend_momentum": 0.0,
        "daily_mover": 0.0,
    },
    # R13-B8: trigger thresholds (TRIGGER_CONFIG["thresholds"], perception.py
    # L272-300). Same registration gap as trigger_weights. Includes the D6
    # fix: trend_momentum_pct is 5.0 here and in TRIGGER_CONFIG — the
    # perception dict.get fallback (L298/300) and the
    # uptrend_momentum/downtrend_momentum signature defaults (triggers.py
    # L397/419) used to silently say 3.0 (the value that over-surfaced 22
    # triggers/scan at ~4.5x AI cost); they are dead fallbacks (runtime is
    # always 5.0 via get_config) but a missing thresholds key would silently
    # resurrect 3.0, so all four now agree. Leaf names snake_case; mapped to
    # camelCase runtime keys by trigger_thresholds_params(). Six keys are
    # floats (> 0 guard), ten are ints (>= 1 guard). Defaults verbatim.
    "trigger_thresholds": {
        "sigma_threshold": 2.0,
        "trend_momentum_lookback": 72,
        "trend_momentum_pct": 5.0,
        "breakout_lookback": 48,
        "breakout_min_rvol": 1.5,
        "breakout_rvol_window": 20,
        "breakout_atr_score_mult": 3.0,
        "breakout_confirm_bars": 2,
        "bb_length": 20,
        "bb_std_dev": 2,
        "adx_period": 14,
        "momentum_lookback": 2,
        "momentum_pct": 4.0,
        "vol_buildup_ratio": 2.5,
        "trend_flip_bars": 3,
        "higher_lows_required": 4,
    },
    # R13-B9: perception scan_budget knobs (perception.py scan_once). Twelve
    # leaves covering the candle-cache size, the per-scan market budget split
    # (total / HIP-3 reservation / movers slots / USD volume floors), the
    # rotating universe sweep, batch rate-limit pacing, thread-pool width, the
    # movers |24h%| cut-off, and the per-future timeout. They were previously
    # read only via os.environ.get(HERMES_*, <literal>) at scan time and never
    # appeared in CANONICAL_DEFAULTS — invisible to dashboard dump /
    # validate_config_updates and un-tunable via the canonical config channel.
    # perception.scan_budget_params() keeps every legacy HERMES_* env var as
    # the top-priority override (MCP server writes HERMES_MAX_MARKETS; existing
    # test/operator knobs keep working), then falls through to this block via
    # cfg_get (HERMES_CFG_SCAN_BUDGET__* env + agent-config). Zero is a legal
    # "reserved disabled" value for budget slots / sweep / sleep; cache size,
    # batch size, the movers % cut-off and the timeout must be >= 1 / > 0.
    # Defaults mirror the perception literals verbatim; behaviour unchanged.
    "scan_budget": {
        "cache_max": 512,
        "max_markets": 60,
        "max_markets_hip3": 25,
        "max_markets_movers": 10,
        "movers_vol_floor_usd": 300_000.0,
        "hip3_movers_floor_usd": 50_000.0,
        "universe_sweep": 0,
        "batch_size": 20,
        "batch_sleep_sec": 0.3,
        "parallel_workers": 32,
        "movers_min_pct": 1.0,
        "future_timeout_sec": 60,
    },
    # R13-B10: research-path LLM call knobs (research.py _call_openrouter /
    # _debate_direct). Eleven leaves covering the gateway model/base URL, the
    # sampling temperature, the normal-path vs debate-path response token
    # budgets, the read/connect httpx timeouts, the 429/5xx retry budget with
    # its exponential backoff base/cap, and the finish_reason=length
    # continuation turn budget. They were previously either local literals
    # inside _call_openrouter (temperature / timeouts / retries / backoff /
    # continuations) or read only via OPENROUTER_* env vars, and never appeared
    # in CANONICAL_DEFAULTS — invisible to dashboard dump /
    # validate_config_updates and un-tunable via the canonical config channel.
    # research.research_llm_params() keeps OPENROUTER_MODEL /
    # OPENROUTER_BASE_URL as the top-priority override (operator gateway
    # routing), then falls through to this block via cfg_get
    # (HERMES_CFG_RESEARCH_LLM__* env + agent-config). OPENROUTER_API_KEY stays
    # a bare secret env var and is deliberately NOT registered here. Defaults
    # mirror the research.py literals verbatim; behaviour unchanged.
    "research_llm": {
        "model": "deepseek-v4-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "temperature": 0.1,
        "max_tokens": 500,
        "debate_max_tokens": 350,
        "timeout_sec": 60.0,
        "connect_timeout_sec": 5.0,
        "retries": 2,
        "backoff_base_sec": 1.0,
        "backoff_cap_sec": 15.0,
        "continuations": 2,
        # Audit 2026-09-03 P0-2: hard per-call cap for the single-LLM
        # fallback path (debate failed -> _call_ai). 0 disables the cap
        # (legacy 60s inheritance). Mirrors research.py literal verbatim.
        "fallback_timeout_sec": 30.0,
    },
    # R13-B10: research-path concurrency / prefetch knobs (research.py
    # _get_pool / _http / _signals_block / _parallel_prefetch). Nine leaves
    # covering the shared ThreadPoolExecutor width, the reused httpx client's
    # keepalive/total connection-pool limits, the inner signals-block future
    # timeout, the per-source prefetch fallback ceiling, and the four
    # per-source fetch ceilings (candles / funding / news / signals). They
    # were previously read only via os.environ.get(HERMES_RESEARCH_*,
    # <literal>) or hardcoded as httpx.Limits(...) literals, and never
    # appeared in CANONICAL_DEFAULTS. research.research_fetch_params() keeps
    # every legacy HERMES_RESEARCH_* env var (including the
    # HERMES_RESEARCH_FETCH_TIMEOUT_<SOURCE> family) as the top-priority
    # override, then falls through to this block via cfg_get
    # (HERMES_CFG_RESEARCH_FETCH__* env + agent-config). Pool width and
    # connection limits are read lazily at pool/client construction, so
    # config changes take effect on the next process (the singletons are
    # built once). Defaults mirror the research.py literals verbatim;
    # behaviour unchanged.
    "research_fetch": {
        "pool_workers": 16,
        "max_connections": 16,
        "max_keepalive_connections": 8,
        "signals_timeout_sec": 40.0,
        "fetch_timeout_default_sec": 45.0,
        "fetch_timeout_candles_sec": 15.0,
        "fetch_timeout_funding_sec": 8.0,
        "fetch_timeout_news_sec": 10.0,
        "fetch_timeout_signals_sec": 12.0,
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
        "save_backoff_factor": 3,
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
    # R13-B4: executor execution-path constants (executor.py). Three gaps:
    #   * tp_atr_mult DRIFT FIX — the key was already registered (above) and
    #     server.py / research.py read it, but the executor's actual TP
    #     placement (_place_tp_scale_out and the maybe_execute final_tp) used
    #     the module constant TP_ATR_MULT=1.0 and NEVER read cfg, so an
    #     operator tuning it to 1.2/1.5 made AI advice / backtest / live
    #     order disagree. The hot path now resolves it via cfg_get.
    #   * sl_ceiling_hard_max_pct — the HYPE-43%-incident hard clamp on the
    #     backup-SL width was a function-local literal 15.0 (executor
    #     L2296), not configurable / observable / env-overridable.
    #   * liq_buffer_usd + execution block — the P0-4 liquidation pre-place
    #     gate threshold (env-only HERMES_LIQ_BUFFER_USD=10) and the HL
    #     taker-fee bookkeeping constants (HERMES_TAKER_FEE_PCT=0.025
    #     env-only; round_trip_fills=2 a pure hardcode) were invisible to
    #     the canonical schema / dashboard / config audit.
    # Legacy env vars (HERMES_LIQ_BUFFER_USD / HERMES_TAKER_FEE_PCT) keep
    # precedence for backward compat; canonical defaults match the existing
    # literals verbatim so behaviour is unchanged when nothing is set.
    "sl_ceiling_hard_max_pct": 15.0,
    "liq_buffer_usd": 10.0,
    "execution": {
        # Hyperliquid perp taker fee in PERCENT (HL = 2.5bps = 0.025%), used
        # to model round-trip entry+exit cost in realized-PnL bookkeeping.
        "taker_fee_pct": 0.025,
        # Number of taker fills modeled per round trip (entry + exit = 2).
        "round_trip_fills": 2,
    },
    # SHADOW-mode paper-trading ledger (shadow_book.py). When mode=SHADOW and a
    # decision passes EVERY risk gate, instead of only returning
    # "shadow_mode_would_execute" the engine books a VIRTUAL fill into an
    # isolated paper account, marks it to live mids each loop, runs the SAME
    # DSL exit policy the live engine uses, and books a virtual close +
    # realized PnL when a stop / target / timeout fires. Nothing here touches
    # the real exchange, real orders, or the real .agent-memory ledger — it is
    # a decision-rehearsal book that lets the dashboard show what the strategy
    # WOULD have done with a configurable virtual bankroll.
    "shadow_book": {
        # Book virtual fills while in SHADOW mode. When false the engine keeps
        # the old "log-only" shadow behaviour (no paper positions).
        "enabled": True,
        # Virtual USDC the paper account starts with (operator-configurable).
        "starting_balance": 10000.0,
        # Per-fill taker fee in PERCENT, modeled on close across the round trip
        # (mirrors execution.taker_fee_pct; HL = 0.025% per fill).
        "taker_fee_pct": 0.025,
        # Number of fills per round trip (entry + exit = 2).
        "round_trip_fills": 2,
        # Cap on concurrent virtual positions (defensive; live max_concurrent
        # already gates entries, this bounds the paper book).
        "max_positions": 10,
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
#     (on-disk read/write/restore keep lenient semantics so hand-edited
#     files and plugin state round-trip; BOTH HTTP write paths enforce
#     strict mode at the patch gate — D-FCFG-4, deep audit 2026-08-28).
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
        # semantics — hand-edited files / restores may carry keys the
        # schema does not know (the HTTP patch gates reject them —
        # D-FCFG-4).
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

    # tmp-in-dir + fsync file + os.replace + fsync dir, with the EBUSY
    # bind-mount in-place rewrite, lives in agents.atomic_io. The caller
    # already holds LOCK_EX on _CONFIG_LOCK_PATH (a second flock in this
    # process would self-deadlock), so we call the unlocked helper directly.
    atomic_io.write_json_atomic(
        CONFIG_PATH, cfg, indent=2, fsync=True, ebusy_fallback=True
    )
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
