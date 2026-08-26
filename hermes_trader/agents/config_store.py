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
import time
from typing import Any, Dict, List, Optional, TypeVar

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

# ---------------------------------------------------------------------------
# Canonical defaults — MUST stay in sync with .agent-config.json.
# These are the fallback values used when a key is missing from the config
# file. They replace the dozens of scattered ``.get(key, <random number>)``
# calls whose defaults had drifted from production (e.g. max_daily_loss_usd
# fell back to -100 in one place while production uses -30).
# ---------------------------------------------------------------------------
CANONICAL_DEFAULTS: Dict[str, Any] = {
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
    "whale_regime_bypass": False,
    "whale_force_execute": False,
    "whale_size_multiplier": 1.0,
    "block_counter_trend_bypass": True,
    "trend_surface_enabled": True,
    "loss_cooldown_min": 180,
    "min_ai_close_hold_min": 25,
    "breakout_force_execute": False,
    "sl_atr_mult": 1.5,
    "min_trend_score": 0.55,
    # Regime classifier thresholds (chop / against-funding conviction bars)
    "chop_min_conf": 0.75,
    "chop_min_score": 55.0,
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
    },
    # Native in-process multi-perspective research debate. Off by default —
    # when enabled, research() runs bull/bear LLM calls in parallel plus an
    # arbiter synthesis with a hard latency cap and a single-LLM fallback on
    # any failure.
    "debate_research": {
        "enabled": False,
        "max_latency_s": 15.0,
        "cache_ttl_s": 300.0,
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
    # Per-coin parameter overrides; deep-merged on top of the base config by
    # with_coin_overrides() / executor. Empty by default.
    "coin_overrides": {},
    # 配置文件注释字段（不参与交易逻辑）
    "_comment": "",
}

# Legacy alias — code that imports DEFAULT_CONFIG gets the full canonical set.
DEFAULT_CONFIG: Dict[str, Any] = CANONICAL_DEFAULTS


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
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


def _lookup_in_dict(d: Dict[str, Any], dotted_key: str) -> Any:
    """Look up a dotted key in an arbitrary dict, raising KeyError if absent."""
    parts = dotted_key.split(".")
    node: Any = d
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            raise KeyError(dotted_key)
        node = node[p]
    return node


def cfg_get(dotted_key: str, default: Any = None, *, config: Optional[Dict[str, Any]] = None) -> Any:
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


def apply_coin_override(config: Dict[str, Any], coin: Optional[str]) -> Dict[str, Any]:
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


def read_agent_config() -> Dict[str, Any]:
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
    return _deep_merge(CANONICAL_DEFAULTS, raw)


def _read_raw_config() -> Optional[Dict[str, Any]]:
    """Read and parse the raw JSON config, or None on any failure."""
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            logger.error(
                f"[config] {CONFIG_PATH} top-level is {type(cfg).__name__}, "
                f"expected object — falling back to CANONICAL_DEFAULTS"
            )
            return None
        return cfg
    except FileNotFoundError:
        logger.warning(f"[config] {CONFIG_PATH} not found — using CANONICAL_DEFAULTS")
        return None
    except json.JSONDecodeError as e:
        logger.error(
            f"[config] {CONFIG_PATH} is CORRUPT (JSON error at line {e.lineno} col "
            f"{e.colno}): {e.msg}. Falling back to CANONICAL_DEFAULTS — "
            f"investigate before trading; do NOT overwrite the file blindly."
        )
        return None
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


def write_agent_config(cfg: Dict[str, Any], *, backup: bool = True) -> None:
    """Write the agent config to .agent-config.json (atomic replace + lock).

    When *backup* is True (default), the previous config is copied to
    ``.agent-config.json.bak`` before overwriting, enabling rollback.

    Under a Docker single-file bind mount, os.replace() onto the mounted
    target fails with EBUSY ("Device or resource busy") because the kernel
    cannot swap the inode a mount point points at. In that case we fall back
    to truncating and rewriting the mounted file in place while still holding
    the exclusive flock, so concurrent readers see either the old or new
    contents rather than a torn file.
    """
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

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
        logger.info(f"[config] written {len(cfg)} keys to {CONFIG_PATH}")
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


def backup_config() -> Optional[Dict[str, Any]]:
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
    """Restore the config from the last backup. Returns True on success."""
    old = backup_config()
    if old is None:
        return False
    write_agent_config(old, backup=False)
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


def create_snapshot(reason: str = "manual") -> Dict[str, Any]:
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


def list_snapshots() -> List[Dict[str, Any]]:
    """Return all manual snapshots, newest first. Each item has id/ts/reason."""
    import glob
    snaps: List[Dict[str, Any]] = []
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
    """Restore a specific manual snapshot by unix timestamp. Returns True."""
    path = _snap_path(ts)
    lock_fd = None
    try:
        lock_fd = os.open(_CONFIG_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not os.path.exists(path):
            return False
        with open(path, "r") as f:
            old = json.load(f)
        # Do not overwrite the rolling .bak with the snapshot itself; a
        # restore is a recovery action, not a normal edit.
        write_agent_config(old, backup=False)
        logger.warning(f"[config] restored from snapshot {path}")
        return True
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[config] snapshot restore failed: {e}")
        return False
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


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
