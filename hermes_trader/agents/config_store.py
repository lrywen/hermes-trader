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
import hashlib
import importlib
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator, Optional, TypeVar

from hermes_trader.agents import atomic_io
from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS

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


# ── CS-E (2026-09-09): 阈值 era 分段归因 ─────────────────────────────
# 仅这些配置路径参与 era 指纹：它们是会改变入场选择 / 风控熔断 / 仓位与
# DSL 退出 / 实验臂行为的阈值。无关键（mode、日志路径、UI 插件等）的改动
# 不产生新 era，避免归因时间线被噪声改动污染。
# - 以 ".*" 结尾：跟踪整个子树（该子树下任一叶变化都换 era）。
# - 其余：跟踪标量或该精确节点。
# 这是 era 归因的单一事实源，离线脚本 scripts/era_attribution.py 复用。
ERA_TRACKED_PATHS: tuple[str, ...] = (
    # 入场选择类
    "min_ai_confidence",
    "max_signal_price_deviation_pct",
    "score_invariant_enabled",
    "scan.minCompositeScore",
    "runner_entry_gate.*",
    "ta_late_entry.*",
    # 风控 / 熔断类
    "market_circuit.*",
    "max_daily_loss_usd",
    # 仓位 / DSL 退出类
    "leverage",
    "equity_fraction_per_trade",
    "dsl_exit.*",
    # 实验臂（off 臂无真实成交，离线仅做 would-be 信号归因）
    "xs_reversal.*",
    "trend_filter_200ma.*",
    "daily_extension_cap.*",
)


def _extract_tracked_subset(
    config: dict[str, Any], paths: tuple[str, ...] = ERA_TRACKED_PATHS
) -> dict[str, Any]:
    """从生效配置视图中抽取 era 跟踪路径的实际值。

    缺失的精确键记为 None；整条子树缺失则跳过（不污染指纹）。
    """
    subset: dict[str, Any] = {}
    for path in paths:
        if path.endswith(".*"):
            prefix = path[:-2]
            try:
                node = _lookup_in_dict(config, prefix)
            except KeyError:
                continue
            subset[prefix] = node
        else:
            try:
                subset[path] = _lookup_in_dict(config, path)
            except KeyError:
                subset[path] = None
    return subset


def _era_id_from_subset(subset: dict[str, Any]) -> str:
    """对跟踪子集做 canonical-JSON SHA-256，返回 12 位短指纹。"""
    blob = json.dumps(subset, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def compute_config_era(
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """计算给定生效配置的 era 指纹（CS-E，纯读、无副作用）。

    返回 ``{"era_id": <12 位 hex>, "tracked": {路径: 值}}``。*config* 为
    None 时读取当前全局生效配置（已与 CANONICAL_DEFAULTS 合并）。per-coin
    调用方应传入经 :func:`apply_coin_override` 合并后的视图，使 era 反映
    该笔下单真正生效的阈值。任何异常都不抛出（记账埋点不得影响下单）。
    """
    try:
        if config is None:
            config = read_agent_config()
        subset = _extract_tracked_subset(config)
        return {"era_id": _era_id_from_subset(subset), "tracked": subset}
    except Exception:  # pragma: no cover - 纯观测，不能阻断交易热路径
        logger.debug("[config] compute_config_era failed", exc_info=True)
        return {"era_id": None, "tracked": {}}
# ---------------------------------------------------------------------------
# Canonical defaults are imported at module top from the pure-data module
# config_defaults (god-module cleanup 2026-09-27). They are the deep-merge base
# read_agent_config falls back to when a key is missing from the live file.
# ---------------------------------------------------------------------------


# Legacy alias — code that imports DEFAULT_CONFIG gets the full canonical set.
DEFAULT_CONFIG: dict[str, Any] = CANONICAL_DEFAULTS

# Audit 2026-09-06 (F4, engineering hygiene): single source of truth for
# .agent-config.json keys that are read ONCE at process start and need a loop
# restart to take effect. This set is intentionally EMPTY today: every
# agent-config key is hot-reloaded — `mode` is re-read per cycle, `enable_crypto`
# is re-read per perception scan (perception.py asset-class toggle block), and
# flipping `enable_hip3` is detected per cycle and triggers an immediate
# universe rebuild (trading_loop.py hot-toggle block). CLI / MCP hint surfaces
# MUST import this instead of hardcoding "restart required" key lists, so the
# hints can never drift ahead of the actual hot-reload behaviour again.
#
# Process-lifetime settings that are NOT agent-config keys live in environment
# variables instead and are documented here so the hint text stays accurate:
#   * HERMES_OPERATOR_TOKEN      — read per HTTP request (rotating it needs no
#                                  restart; see dashboard.py request-time check)
#   * HERMES_MCP_ALLOW_WRITE     — read once at MCP server process start
#   * HYPERLIQUID_PRIVATE_KEY    — read once at loop / executor process start
#   * HERMES_SKIP_STARTUP_SAFETY — read once at loop startup
STARTUP_ONLY_KEYS: frozenset[str] = frozenset()


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


# P1-14 (audit 2026-09-04): startup SAFETY ENVELOPE. Canonical defaults now
# match production, but a hand-edited config (or a future code regression that
# loosens a default) can still set a risk-critical key to an out-of-envelope
# value — e.g. max_concurrent=20 or max_trade_notional_usd=10_000 on a $20
# account. Schema range checks only catch TYPE/absurd errors, not "this value
# is 5–27× looser than the production risk posture". These bounds are the
# conservative envelope; BREACHING them refuses to trade rather than silently
# running with ballooned exposure. Pure function for offline tests.
#   (dotted_key, operator, bound, human) — operator in {">", ">=", "<", "<="};
#   the check FAILS if cfg value compares past the bound away from safety.
_STARTUP_SAFETY_CHECKS: list[tuple[str, str, float, str]] = [
    ("max_trade_notional_usd", ">", 500.0,
     "per-trade notional cap > $500 is far beyond the production micro-book"),
    ("max_concurrent", ">", 6,
     "more than 6 concurrent positions is beyond the production book (live: 2)"),
    ("max_total_notional_pct", ">", 10.0,
     "total-open-notional > 10× equity exceeds the leverage band"),
    ("leverage", ">", 20,
     "leverage > 20x far exceeds production sizing (live: 10)"),
    ("atr_risk_sizing.risk_per_trade_pct", ">", 0.05,
     "atr_risk_sizing.risk_per_trade_pct > 5% risks >5% equity on one stop"),
    ("min_ai_confidence", "<", 0.50,
     "min_ai_confidence < 0.50 admits coin-flip verdicts as entries"),
    ("max_daily_loss_usd", "<", -50.0,
     "daily USD loss floor looser than -$50 on the production book"),
]


def startup_config_integrity_errors(cfg: dict[str, Any]) -> list[str]:
    """Check the MERGED config *cfg* against the P1-14 startup safety envelope.

    Returns a list of human-readable error strings (empty when the config is
    safe to run with). Called by the trading loop BEFORE the first scan; a
    non-empty result must stop the loop (refuse to trade) rather than fall
    back to defaults mid-run. Uses the same dotted-key lookup as cfg_get so
    nested keys are resolved against the merged view.
    """
    errors: list[str] = []
    for dotted, op, bound, human in _STARTUP_SAFETY_CHECKS:
        try:
            val = _lookup_in_dict(cfg, dotted)
        except Exception as e:
            # H4 [2026-09-05]: 键路径缺失/无法解析时显式告警，避免静默跳过
            # 安全检查（静默跳过会让 P1-14 防御层形同虚设）
            logger.error("startup_safety_check key path unresolvable: %s err=%s", dotted, e)
            val = None
        if val is None:
            # Missing keys resolve to canonical defaults via cfg_get at use
            # sites, and the canonical values are within envelope — not a
            # startup error here.
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            errors.append(f"startup safety: {dotted}={val!r} is not numeric ({human})")
            continue
        breach = (
            (op == ">" and num > bound)
            or (op == ">=" and num >= bound)
            or (op == "<" and num < bound)
            or (op == "<=" and num <= bound)
        )
        if breach:
            errors.append(
                f"startup safety: {dotted}={num} {op} {bound} — {human}")

    # P2-20 (audit 2026-09-04): SHADOW/LIVE position-cap PARITY. An explicit
    # shadow_book.max_positions that diverges from max_concurrent makes the
    # paper book admit a different number of concurrent positions than the
    # live gate allows — shadow results then stop tracking live 1:1 (the
    # production config once shipped max_concurrent=4 vs max_positions=2).
    # The fix is to DELETE shadow_book.max_positions (it then tracks the
    # live cap automatically); an explicit mismatch is a startup refusal.
    shadow_cap = None
    try:
        shadow_cap = _lookup_in_dict(cfg, "shadow_book.max_positions")
    except Exception:
        shadow_cap = None
    if shadow_cap is not None:
        try:
            live_cap = int(float(_lookup_in_dict(cfg, "max_concurrent")))
            shadow_cap_i = int(float(shadow_cap))
        except Exception:
            errors.append(
                "startup safety: shadow_book.max_positions/max_concurrent "
                "are not numeric — cannot verify SHADOW/LIVE position parity")
        else:
            if shadow_cap_i != live_cap:
                errors.append(
                    f"startup safety: shadow_book.max_positions={shadow_cap_i} "
                    f"!= max_concurrent={live_cap} — SHADOW/LIVE position caps "
                    "diverge; delete shadow_book.max_positions so it tracks the "
                    "live cap (shadow_book.py:_max_positions)")

    # B-10：noise_band 强制开启断言。统一技术改造文档 §2.7 的 13 项里，
    # noise_band 是**唯一**经得起月度符号一致性检验的正贡献（生产权威配置
    # /data 为 enabled=true/atr_mult=0.8）。误关会让 sub-first-tier 的回踩
    # 直接触发退出、回测与实盘出场口径分叉。
    #
    # 该守卫仅在生产权威源（/data/.agent-config.json）上强制：canonical
    # 代码默认 noise_band.enabled=true（已与生产对齐，atr_mult=0.8）；守卫仍
    # 只读取 /data 原始文件，防止**生产**配置把它从开启状态误关，本地/CI 不报错。
    errors.extend(_production_noise_band_errors())

    # 灰度 enforce fail-closed 守卫（market_circuit / daily_extension_cap）：
    # 这两项 canonical 保持 shadow（新部署惰性），但生产 /data 已切 enforce。
    # 与改 canonical 不同，这里用守卫保证：生产基线既为 enforce，配置被截断/
    # 改弱时拒绝启动，而不是静默回落到 canonical 的 shadow（保护被关）。
    errors.extend(_production_enforce_arm_errors())

    # B-7：配置来源强制断言。生产权威配置＝容器挂载卷 /data/.agent-config.json
    # （2026-09 为 142 顶键）。当实际加载路径就是 /data 权威文件时，校验其
    # 关键键齐全度，防止读到被截断/写错的配置而静默运行。非 /data 部署
    # （本地开发/CI）不强制键数，避免误报。
    errors.extend(_authoritative_config_errors())
    return errors


def _running_from_data_config() -> bool:
    return os.path.abspath(CONFIG_PATH) == "/data/.agent-config.json"


def _production_noise_band_errors() -> list[str]:
    """B-10：仅在生产权威 /data 源上要求 noise_band 保持开启。canonical
    默认已 enabled=true；非 /data 部署（本地/CI）不检查。"""
    if not _running_from_data_config():
        return []
    try:
        raw = _read_raw_config()
        noise = ((raw or {}).get("dsl_exit") or {}).get("noise_band")
        if isinstance(noise, dict) and noise.get("enabled", True) is False:
            return [
                "startup safety: dsl_exit.noise_band.enabled=false on the production "
                "/data config — noise_band is the only validated positive-contribution "
                "exit gate (§2.7) and must stay on; set HERMES_SKIP_STARTUP_SAFETY=1 "
                "to deliberately override"]
    except Exception as e:
        logger.error("B-10 noise_band check failed (non-fatal): %s", e)
    return []


# 灰度臂：canonical 口径（新部署惰性）与生产 /data 基线（已 enforce）。
_GRAY_ENFORCE_ARMS: tuple[tuple[str, str], ...] = (
    ("market_circuit", "market_circuit"),
    ("daily_extension_cap", "daily_extension_cap"),
)


def _production_enforce_arm_errors() -> list[str]:
    """生产 /data 基线已切 enforce 的灰度臂，必须保持 enforce。

    canonical 仍 shadow 是有意的（新部署默认惰性）；但生产一旦确立 enforce，
    配置丢键/被截断而深合并回落到 shadow 会静默关闭保护。本守卫在权威 /data
    源上检测这些块的 mode 弱于 enforce 即拒绝启动（fail-closed）。非 /data
    部署（本地/CI）不检查。
    """
    if not _running_from_data_config():
        return []
    errors: list[str] = []
    try:
        raw = _read_raw_config()
        for block, human in _GRAY_ENFORCE_ARMS:
            blk = (raw or {}).get(block)
            mode = blk.get("mode") if isinstance(blk, dict) else None
            if mode != "enforce":
                errors.append(
                    f"startup safety: {human}.mode={mode!r} on the production /data "
                    f"config — production baseline is enforce; a weaker mode would "
                    f"silently disable the gate on a key-loss fallback to canonical "
                    f"shadow. Restore enforce (or set HERMES_SKIP_STARTUP_SAFETY=1 to "
                    f"deliberately downgrade)")
    except Exception as e:
        logger.error("gray enforce-arm check failed (non-fatal): %s", e)
    return errors


def _authoritative_config_errors() -> list[str]:
    """B-7：当从生产权威路径 /data/.agent-config.json 加载时，校验文件可解析
    且关键顶键齐全。仅在权威路径存在且为本进程 CONFIG_PATH 时生效；本地/CI
    不挂载该路径时返回空（开发口径）。任何读取异常都降级为安全告警而非阻断，
    以避免文件系统瞬态问题误杀启动。
    """
    try:
        if not _running_from_data_config():
            return []
        if not os.path.exists(CONFIG_PATH):
            return ["startup safety: authoritative config /data/.agent-config.json "
                    "is CONFIG_PATH but missing"]
        # 关键键：缺失任一即说明配置被截断，不能按生产风险姿态运行。
        _REQUIRED_KEYS = (
            "mode", "leverage", "max_concurrent", "max_trade_notional_usd",
            "max_daily_loss_usd", "dsl_exit", "runner_entry_gate",
        )
        raw = _read_raw_config()
        if not isinstance(raw, dict):
            return ["startup safety: authoritative /data config is not a JSON object"]
        missing = [k for k in _REQUIRED_KEYS if k not in raw]
        if missing:
            return [f"startup safety: authoritative /data config missing required "
                    f"top-level keys: {', '.join(missing)}"]
    except Exception as e:  # 读取/解析瞬态：告警但不误杀
        logger.error("B-7 authoritative config check failed (non-fatal): %s", e)
    return []


def startup_safety_bypass_acked() -> bool:
    """True when the operator has explicitly acked a startup safety breach.

    The loop MUST NOT read HERMES_SKIP_STARTUP_SAFETY directly (all HERMES_*
    knobs resolve through helpers so the env surface stays registered in one
    place). Set HERMES_SKIP_STARTUP_SAFETY=1 to deliberately run a config that
    breaches the conservative envelope; the breaches are still logged at
    CRITICAL.
    """
    import os

    return os.environ.get("HERMES_SKIP_STARTUP_SAFETY") == "1"


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


def _reify_none_defaults(defaults: dict[str, Any], view: dict[str, Any]) -> None:
    """Re-materialize, recursively into *view*, every canonical key whose
    default is ``None`` and that is absent from *view*.

    CS-D verdict 5 (2026-09-08): for config-diff purposes an explicit null
    and a missing key are the SAME effective state ("unset → scale off
    max_latency_s"), which is exactly how the runtime read path treats them
    (``d.get(k)`` + ``is not None`` guards). ``_deep_merge`` uses None as a
    deletion marker, so a full RMW view carrying explicit None stubs (e.g.
    ``debate_research.bull_timeout_s: null``) loses them on re-merge while a
    sparse .bak keeps the defaults' stubs — the two effective views then
    differ in JSON alone and every no-op RMW write falsely reports the
    enclosing section in ``changed_keys`` (the stubs oscillate null/missing
    on each full-view write). Re-materializing the canonical None defaults
    on BOTH sides normalizes the diff without touching runtime semantics:
    nothing here is ever read by the trading loop, only serialized for
    comparison. Operators cannot set these keys to null either — validation
    treats a type collision (dict-valued default overridden with null) as a
    schema error — so missing vs null carries no operator signal to lose.
    """
    for key, default in defaults.items():
        if isinstance(default, dict):
            if key not in view:
                view[key] = {}
            if isinstance(view.get(key), dict):
                _reify_none_defaults(default, view[key])
        elif default is None and key not in view:
            view[key] = None


def _effective_view_for_diff(raw: dict[str, Any]) -> dict[str, Any]:
    """Canonical effective config view for write-audit diffs ONLY.

    Same merge as the runtime read path (:func:`read_agent_config`) plus
    :func:`_reify_none_defaults`, applied identically to the prior (.bak)
    and the just-written views so no-op RMW writes report an empty
    ``changed_keys`` instead of false None-stub noise.
    """
    view = _deep_merge(CANONICAL_DEFAULTS, raw)
    _reify_none_defaults(CANONICAL_DEFAULTS, view)
    return view


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


def live_trading_authorized() -> bool:
    """Explicit live-money safety gate (P0-1, 2026-09-12).

    A config file with ``mode=LIVE`` alone must never be sufficient to place
    real orders after a fresh deploy / stale mount / copied config: the
    operator must additionally opt in via HERMES_ENABLE_LIVE=true in the
    process environment. Fail-closed: absent or any value other than
    1/true/yes/on (case-insensitive) denies LIVE entries. Exits (reduce-only
    flatten, stop/trigger orders, kill-switch de-risking) never call this.
    """
    return str(os.environ.get("HERMES_ENABLE_LIVE", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


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


def _emit_config_write_audit(
    *, via: str, new_cfg: dict[str, Any], backup: bool
) -> None:
    """CS-A (2026-09-08): emit a best-effort ``config_write`` session-log
    event after a successful on-disk config write.

    Before CS-A the ONLY config write that produced an audit record was the
    dashboard HTTP API (``config_update``); direct writes from the MCP
    server, CLI, weekly calibration script, backup/snapshot restores, or a
    hand-held python session left no trace. The file-integrity watchers
    (hermes-config-watch) DO see the file change, so a config file mutation
    without a matching audit event is now an actionable incident signal.
    The ``via`` label identifies the write path (e.g. ``mcp``,
    ``weekly_calibrate``, ``cli``, ``web_api``, ``restore_backup``,
    ``unknown``); ``changed_keys`` is the set of top-level keys whose
    normalized JSON differs from the pre-write raw file. This function must
    NEVER raise — auditing cannot be allowed to break a valid write.
    """
    try:
        # Called AFTER the write: the on-disk file is already the new cfg,
        # so the prior state is reconstructed from the .bak just refreshed
        # by _write_raw_locked. When backup=False, changed_keys is reported
        # as null (the caller chose not to keep the prior state).
        prior = None
        if backup:
            try:
                with open(_BACKUP_PATH, "r") as f:
                    prior = json.load(f)
            except (OSError, json.JSONDecodeError):
                prior = None
        new_eff = _effective_view_for_diff(new_cfg)
        # CS-E: 计算写入前后的 era 指纹，作为离线 era 分段的边界信号。
        new_era = _era_id_from_subset(_extract_tracked_subset(new_eff))
        changed: Optional[list[str]]
        old_changed: Optional[dict[str, Any]]
        new_changed: Optional[dict[str, Any]]
        prev_era: Optional[str]
        if prior is not None:
            # Compare EFFECTIVE views: the .bak holds the raw (sparse,
            # hand-editable) file while the just-written cfg is usually the
            # full CANONICAL_DEFAULTS-merged view (update_agent_config RMW).
            # Normalize both through the same merge so only real operator
            # changes show up in changed_keys. CS-D verdict 5: the diff view
            # also re-materializes canonical None defaults (null ≡ missing),
            # otherwise the RMW full view's explicit null stubs —
            # debate_research.bull_timeout_s / synth_timeout_s — get deleted
            # by _deep_merge on one side only and every no-op write cries
            # wolf on the enclosing section.
            prior_eff = _effective_view_for_diff(prior)
            prev_era = _era_id_from_subset(_extract_tracked_subset(prior_eff))
            keys = sorted(set(prior_eff) | set(new_eff))
            changed = sorted(
                k for k in keys
                if json.dumps(prior_eff.get(k), sort_keys=True, default=str)
                != json.dumps(new_eff.get(k), sort_keys=True, default=str)
            )
            # CS-E: 携带发生变化的顶层键的 old/new 完整值，供 era 边界严格
            # 重建；体量受限于本次实际改动的键。
            old_changed = {k: prior_eff.get(k) for k in changed}
            new_changed = {k: new_eff.get(k) for k in changed}
        else:
            changed = None
            old_changed = None
            new_changed = None
            prev_era = None
        from hermes_trader import session_log

        session_log.append({
            "event": "config_write",
            "via": str(via or "unknown"),
            "path": CONFIG_PATH,
            "backup": bool(backup),
            "changed_keys": changed,
            "key_count": len(new_cfg),
            "old": old_changed,
            "new": new_changed,
            "prev_era_id": prev_era,
            "era_id": new_era,
        })
    except Exception:  # pragma: no cover - audit must never break the write
        logger.debug("[config] config_write audit emit failed", exc_info=True)


def write_agent_config(
    cfg: dict[str, Any], *, backup: bool = True, via: str = "unknown"
) -> None:
    """Write the agent config to .agent-config.json (atomic replace + lock).

    F20: thin flock wrapper around :func:`_write_raw_locked` so the write
    body can run inside the exclusive lock already held by
    :func:`update_agent_config` (a second LOCK_EX fd in the same process
    would self-deadlock — flock binds to the open file description).

    CS-A: *via* labels the write path in the best-effort ``config_write``
    audit event emitted after a successful persist.
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
        _emit_config_write_audit(via=via, new_cfg=cfg, backup=backup)
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
def update_agent_config(
    *, backup: bool = True, via: str = "unknown"
) -> Iterator[dict[str, Any]]:
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
        # T-01 (DEF-01): a runtime flip to mode=LIVE MUST pass the same B-13
        # acceptance gate enforced at boot. This is the single choke point for
        # every config write (web PATCH, terminal resume/live, legacy merge),
        # so no write path can arm LIVE without a valid §5.2 record. Raising
        # here aborts before _write_raw_locked → the on-disk mode key is
        # byte-level unchanged (INV-01). Non-LIVE modes are unaffected; a
        # LIVE cfg that already carries a valid record passes through.
        if str(cfg.get("mode", "OFF")).upper() == "LIVE":
            # Test-only escape: the offline suite has many audit/cleanup tests
            # that write mode=LIVE to prove later machinery and carry no B-13
            # record. N-1 hardening: the env flag alone is a silent footgun if
            # it leaks into production. Require BOTH the env flag AND a live
            # pytest process ("pytest" loaded in sys.modules), and emit a
            # WARNING when the hatch actually engages so a bypass is never
            # invisible. Gate-write tests leave the flag unset.
            _hatch_armed = (
                os.environ.get("HERMES_TEST_ALLOW_LIVE_WRITE") == "1"
                and "pytest" in sys.modules
            )
            if (
                os.environ.get("HERMES_TEST_ALLOW_LIVE_WRITE") == "1"
                and "pytest" not in sys.modules
            ):
                logger.warning(
                    "[startup gate] HERMES_TEST_ALLOW_LIVE_WRITE=1 set but no "
                    "pytest process detected — IGNORING the live-write escape "
                    "(B-13 guard stays armed); this flag is for the offline "
                    "test suite only")
            if not _hatch_armed:
                from hermes_trader.agents.live_gate import live_entry_runtime_error
                _live_err = live_entry_runtime_error(cfg)
                if _live_err is not None:
                    raise RuntimeError(
                        "[config] refusing to write mode=LIVE: no valid B-13 "
                        "acceptance record (§5.2 outcome A: 判据1–5 pass + block "
                        "bootstrap 95% CI strictly > 0, bound to the current "
                        "config). Keep mode=SHADOW until the gate record is "
                        f"present at the gate path. (reason={_live_err})"
                    )
            else:
                logger.warning(
                    "[config] B-13 write guard BYPASSED via "
                    "HERMES_TEST_ALLOW_LIVE_WRITE under pytest (test only)")
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
        _emit_config_write_audit(via=via, new_cfg=cfg, backup=backup)
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
        write_agent_config(old, backup=False, via="restore_backup")
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
            write_agent_config(old, backup=False, via="restore_snapshot")
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


# ── Effective-config inspection CLI (audit 2026-09-11, Q14) ─────────────────
# Values can come from several layers (HERMES_CFG_* env, the mounted JSON file,
# CANONICAL_DEFAULTS, or a caller default) and the live file differs from the
# in-repo one, so "which value is actually in effect and why" previously needed
# manual source archaeology. This reuses the exact cfg_get resolution path.
def _resolve_provenance(dotted_key: str) -> tuple[Any, str]:
    """Return (effective_value, source) for one dotted key without changing any
    resolution semantics. Source is one of cfg_env/file/default/unknown."""
    env_raw = _env_override(dotted_key)
    if env_raw is not None:
        try:
            type_hint = type(_lookup_default(dotted_key))
        except KeyError:
            type_hint = str
        return _coerce(env_raw, type_hint), "cfg_env"
    raw = _read_raw_config()
    if raw is not None:
        try:
            return _lookup_in_dict(raw, dotted_key), "file"
        except KeyError:
            pass
    try:
        return _lookup_default(dotted_key), "default"
    except KeyError:
        return None, "unknown"


# Gray-release MODE switches that bypass the generic HERMES_CFG_ scheme
# (each is read by its own module accessor: executor._atr_calib_config /
# _sizing_v2_config / _confidence_decay_config and perception._age_decay_config).
# Included in the startup effective-config snapshot so non-canonical overrides
# are visible too. P1-4 Phase 0: the 4th mode (signal_age_decay) was previously
# missing and silently invisible.
_GRAY_MODE_ENV_KEYS: tuple[str, ...] = (
    "HERMES_CONFIDENCE_DECAY_MODE",
    "HERMES_ATR_REGIME_CALIB_MODE",
    "HERMES_SIZING_V2_MODE",
    "HERMES_SIGNAL_AGE_DECAY_MODE",
)

# Dedicated kill/observability switches read directly by runtime modules,
# also bypassing HERMES_CFG_ and absent from CANONICAL_DEFAULTS. Snapshotted
# even when unset — two of these default ON, so a missing env line does not
# mean the switch is off. (env name, effective default when unset).
_DEDICATED_ENV_SWITCHES: tuple[tuple[str, bool], ...] = (
    ("HERMES_HL_RATE_STATS", True),             # client/rate_limit.py
    ("HERMES_PRICE_CROSSCHECK_ENABLED", True),  # client/price_crosscheck.py
    ("HERMES_IP_DRIFT_WATCH", False),           # scripts/ip_drift_watch.py
)

# ── P1-4 Phase 0: gray-release mode env-vs-config drift observability ───────
_LEGACY_MODE_DRIFT_WARNED: set[str] = set()


def reset_legacy_mode_drift_warnings() -> None:
    """Clear the per-process one-time drift alarms (tests / explicit reset)."""
    _LEGACY_MODE_DRIFT_WARNED.clear()


def report_legacy_mode_drift(
    *,
    env_name: str,
    env_mode: str,
    file_key: str,
    file_mode: str,
    valid_modes: tuple[str, ...],
) -> None:
    """One-time alarm when a dedicated gray-release MODE env var actively
    overrides the persisted config (env wins; a container recreate without the
    env silently reverts to the file value). ``file_mode`` must be the fully
    resolved file value ("off" when absent/invalid, including legacy-bool
    fallbacks). Observability only: logs once per process and appends a
    config_env_drift session event; never raises and never affects resolution.
    """
    if env_name in _LEGACY_MODE_DRIFT_WARNED:
        return
    if not env_mode or env_mode not in valid_modes or file_mode == env_mode:
        return
    _LEGACY_MODE_DRIFT_WARNED.add(env_name)
    logger.warning(
        "[config] gray-release mode drift: %s=%r (env, ACTIVE) differs from "
        "%s=%r (config file). Env override wins, but a container recreate "
        "without the env silently reverts to the file value — reconcile "
        ".env.local vs .agent-config.json.",
        env_name, env_mode, file_key, file_mode or "<unset>",
    )
    try:
        from hermes_trader import session_log

        session_log.append({
            "event": "config_env_drift",
            "key": file_key,
            "env_name": env_name,
            "env_value": env_mode,
            "config_value": file_mode,
            "effective": env_mode,
        })
    except Exception:  # audit must never break the caller
        try:
            from hermes_trader.metrics import SWALLOWED_ERRORS
            SWALLOWED_ERRORS.labels(func="config_env_drift_audit").inc()
        except Exception:
            pass
        logger.warning("[config] config_env_drift audit append failed",
                       exc_info=True)


def _effective_config_snapshot_path() -> str:
    """Destination of the startup resolved-config snapshot.

    Defaults to ``runtime_config.effective.json`` next to the mounted config
    (``/data`` in the container); override with
    HERMES_EFFECTIVE_CONFIG_SNAPSHOT. Set to an empty string to disable.
    """
    return os.environ.get(
        "HERMES_EFFECTIVE_CONFIG_SNAPSHOT",
        os.path.join(os.path.dirname(CONFIG_PATH), "runtime_config.effective.json"),
    )


# ── P1-4 Phase 2.2 (plan a): D-family accessor effective-value projection ──
# The legacy HERMES_* env vars stay the highest-priority OPERATOR EMERGENCY
# ESCAPE HATCH for every leaf below — nothing here removes, renames or demotes
# them. The startup effective-config snapshot is the authoritative surface
# for the value actually in effect: each leaf projects the REAL accessor
# output plus why it is active (env / cfg_env / file / default). Three
# families (hl_client_io / hl_rate_limit / dsl_state_io) resolve at IMPORT
# time with config={} into frozen module constants, so the mounted config
# file is structurally invisible to their hot path — those leaves can never
# be labeled "file". All helpers are pure reads and never raise.
def _cfg_layer_source(
    dotted: str,
    raw: Optional[dict[str, Any]],
    *,
    allow_file: bool = True,
) -> str:
    """Label the generic cfg_get layers for one leaf: HERMES_CFG_* env wins,
    then a key present in the mounted raw config file, else the literal."""
    if _env_override(dotted) is not None:
        return "cfg_env"
    if allow_file and raw is not None:
        try:
            _lookup_in_dict(raw, dotted)
            return "file"
        except KeyError:
            pass
    return "default"


def _legacy_env_accepted(raw_env: Optional[str], kind: str, min_v: float) -> bool:
    """Mirror the spec accessors' legacy-env acceptance: strings only need to
    be non-empty, bools accept any non-empty token, ints/floats must coerce
    and clear the per-leaf minimum guard (a rejected env falls through)."""
    if raw_env is None or raw_env == "":
        return False
    if kind in ("s", "b"):
        return True
    try:
        v = int(raw_env) if kind == "i" else float(raw_env)
    except (TypeError, ValueError):
        return False
    return v >= min_v


def _env_then_cfg_source(
    dotted: str,
    legacy_env: Optional[str],
    kind: str,
    raw: Optional[dict[str, Any]],
    *,
    allow_file: bool = True,
) -> str:
    """Source for an ``os.environ.get(...) or cfg_get(...)``-style leaf
    without a min guard: a non-empty coercible legacy env wins, else the
    generic cfg layers."""
    if legacy_env is not None and _legacy_env_accepted(
        os.environ.get(legacy_env), kind, float("-inf")
    ):
        return "env"
    return _cfg_layer_source(dotted, raw, allow_file=allow_file)


def _spec_leaf_source(
    dotted: str,
    legacy_env: Optional[str],
    kind: str,
    min_v: float,
    raw: Optional[dict[str, Any]],
    *,
    allow_file: bool = True,
) -> str:
    """Source for one spec-driven family leaf (research / hl / http /
    memory): accepted legacy env, else the generic cfg layers."""
    if legacy_env is not None and _legacy_env_accepted(
        os.environ.get(legacy_env), kind, min_v
    ):
        return "env"
    return _cfg_layer_source(dotted, raw, allow_file=allow_file)


def _live_global_leaf_source(
    dotted: str,
    legacy_env: Optional[str],
    kind: str,
    min_v: float,
    raw: Optional[dict[str, Any]],
) -> str:
    """Source for dashboard dip_ratio / dip_window. Unlike plain spec leaves,
    the accessor SKIPS a cfg/file candidate equal (or uncoercible) to the
    canonical literal — the live module global remains the active value, so
    such candidates are labeled ``default``."""
    if legacy_env is not None and _legacy_env_accepted(
        os.environ.get(legacy_env), kind, min_v
    ):
        return "env"
    cfg_raw = _env_override(dotted)
    if cfg_raw is not None:
        layer: Optional[str] = "cfg_env"
        candidate: Any = cfg_raw
    elif raw is not None:
        try:
            candidate = _lookup_in_dict(raw, dotted)
        except KeyError:
            return "default"
        layer = "file"
    else:
        return "default"
    try:
        active = int(candidate) if kind == "i" else float(candidate)
        baseline = int(_lookup_default(dotted)) if kind == "i" \
            else float(_lookup_default(dotted))
    except (TypeError, ValueError, KeyError):
        return "default"
    return layer if active != baseline else "default"


def _accessor_effective_view() -> dict[str, Any]:
    """Resolve knobs that bypass the generic provenance walk through their
    REAL module accessors (P1-4 Phase 0 / P0-3 / Phase 2.2). The canonical
    cfg_env/file/default walk cannot see (a) the nineteen loop_runtime
    HERMES_* legacy env vars, (b) the four dedicated gray-release MODE env
    vars, or (c) the nine D-family dual-track accessors — this view reports
    what the running code actually gets. Legacy HERMES_* env stays the
    operator emergency escape hatch (highest priority); this view is the
    read-only authoritative surface for the active value and its source.
    Lazy imports keep the config_store ← client/agent edge cycle-free. Pure
    read; a failing section becomes an {"error": ...} leaf, never raises.
    """
    view: dict[str, Any] = {}
    try:
        from hermes_trader import loop_runtime

        view["loop_runtime"] = loop_runtime.loop_runtime_params()
    except Exception as e:
        view["loop_runtime"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader.agents import executor, perception

        cfg = read_agent_config()
        view["gray_modes"] = {
            "atr_regime_calibration.mode":
                executor._atr_calib_config(cfg)["mode"],
            "confidence_decay.mode":
                executor._confidence_decay_config(cfg)["mode"],
            "atr_risk_sizing.sizing_v2_mode":
                executor._sizing_v2_config(cfg)["mode"],
            "signal_age_decay.mode":
                perception._age_decay_config(cfg)["mode"],
        }
    except Exception as e:
        view["gray_modes"] = {"error": f"{type(e).__name__}: {e}"}

    # ── P1-4 Phase 2.2 (plan a): nine D-family sections. Every section
    # projects the value the running code actually consumes (the REAL accessor
    # output per call, or the frozen module constant for import-time families)
    # plus a per-leaf source label. Sections are independent: each lazy-imports
    # its own module in its own try, so one broken import degrades only that
    # section to {"error": ...}. Legacy HERMES_* env remains the highest-
    # priority operator escape hatch — nothing here changes resolution.
    try:
        raw = _read_raw_config()
    except Exception:
        raw = None
    try:
        from hermes_trader.agents import research

        llm_values = research.research_llm_params()
        view["research_llm"] = {
            leaf: {
                "value": llm_values[leaf],
                "source": _spec_leaf_source(
                    f"research_llm.{leaf}", legacy_env, kind, min_v, raw
                ),
            }
            for leaf, (legacy_env, kind, min_v)
            in research._RESEARCH_LLM_SPEC.items()
        }
    except Exception as e:
        view["research_llm"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader.agents import research

        fetch_values = research.research_fetch_params()
        view["research_fetch"] = {
            leaf: {
                "value": fetch_values[leaf],
                "source": _spec_leaf_source(
                    f"research_fetch.{leaf}", legacy_env, kind, min_v, raw
                ),
            }
            for leaf, (legacy_env, kind, min_v)
            in research._RESEARCH_FETCH_SPEC.items()
        }
    except Exception as e:
        view["research_fetch"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader.client import rate_limit

        io_values = dict(rate_limit._HL_CLIENT_IO)
        view["hl_client_io"] = {
            leaf: {
                "value": io_values[leaf],
                # Frozen at import with config={}: the mounted file is
                # structurally invisible, never label "file".
                "source": _spec_leaf_source(
                    f"hl_client_io.{leaf}", legacy_env, kind, min_v,
                    None, allow_file=False,
                ),
            }
            for leaf, (legacy_env, kind, min_v)
            in rate_limit._HL_CLIENT_IO_SPEC.items()
        }
    except Exception as e:
        view["hl_client_io"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader.client import rate_limit

        rl_values = dict(rate_limit._HL_RATE_LIMIT)
        rl_section: dict[str, Any] = {}
        for leaf, (legacy_env, kind, min_v) in rate_limit._HL_RATE_LIMIT_SPEC.items():
            if leaf == "rate_per_endpoint_gate":
                # The ONE call-time env read in the import-time families:
                # a non-empty env (any token) wins immediately; unset/empty
                # falls back to the frozen import-time constant.
                gate_raw = os.environ.get("HERMES_HL_RATE_PER_ENDPOINT_GATE")
                if gate_raw is not None and gate_raw.strip() != "":
                    source = "env"
                else:
                    source = _spec_leaf_source(
                        "hl_rate_limit.rate_per_endpoint_gate",
                        legacy_env, kind, min_v, None, allow_file=False,
                    )
                value = rate_limit._per_endpoint_gate_enabled()
            else:
                source = _spec_leaf_source(
                    f"hl_rate_limit.{leaf}", legacy_env, kind, min_v,
                    None, allow_file=False,
                )
                value = rl_values[leaf]
            rl_section[leaf] = {"value": value, "source": source}
        view["hl_rate_limit"] = rl_section
    except Exception as e:
        view["hl_rate_limit"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader import dashboard

        http_values = dashboard._http_cache_params()
        view["http_cache"] = {
            leaf: {
                "value": http_values[leaf],
                "source": _spec_leaf_source(
                    f"http_cache.{leaf}", legacy_env, kind, min_v, raw
                ),
            }
            for leaf, (legacy_env, kind, min_v)
            in dashboard._HTTP_CACHE_SPEC.items()
        }
    except Exception as e:
        view["http_cache"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        # importlib (not `from ... import`): a None sys.modules entry must
        # reliably raise so this section alone degrades to {"error": ...}.
        memory = importlib.import_module("hermes_trader.agents.memory")

        mem_values = memory._memory_quality_params()
        view["memory_quality"] = {
            leaf: {
                "value": mem_values[leaf],
                "source": _spec_leaf_source(
                    f"memory_quality.{leaf}", legacy_env, kind, min_v, raw
                ),
            }
            for leaf, (legacy_env, kind, min_v)
            in memory._MEMORY_QUALITY_SPEC.items()
        }
    except Exception as e:
        view["memory_quality"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader import dashboard

        eq_values = dashboard._dashboard_equity_params()
        eq_section: dict[str, Any] = {}
        for leaf, (legacy_env, kind, min_v) in dashboard._DASHBOARD_EQUITY_SPEC.items():
            if leaf in dashboard._DASHBOARD_EQUITY_LIVE_GLOBAL_LEAVES:
                # A cfg/file candidate equal to the canonical literal is
                # skipped by the accessor; the live module global stays active.
                source = _live_global_leaf_source(
                    f"dashboard_equity.{leaf}", legacy_env, kind, min_v, raw
                )
            else:
                source = _spec_leaf_source(
                    f"dashboard_equity.{leaf}", legacy_env, kind, min_v, raw
                )
            eq_section[leaf] = {"value": eq_values[leaf], "source": source}
        view["dashboard_equity"] = eq_section
    except Exception as e:
        view["dashboard_equity"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader.agents import dsl_exit

        # All six are frozen at import with config={} (allow_file=False);
        # five keep the legacy `env or cfg_get` form, the backoff factor has
        # no legacy env channel.
        view["dsl_state_io"] = {
            "save_min_interval_sec": {
                "value": dsl_exit._MIN_SAVE_INTERVAL_SEC,
                "source": _env_then_cfg_source(
                    "dsl_state_io.save_min_interval_sec",
                    "HERMES_DSL_SAVE_INTERVAL_SEC", "f", None, allow_file=False),
            },
            "force_load_ttl_s": {
                "value": dsl_exit._FORCE_LOAD_TTL_S,
                "source": _env_then_cfg_source(
                    "dsl_state_io.force_load_ttl_s",
                    "HERMES_DSL_FORCE_LOAD_TTL_S", "f", None, allow_file=False),
            },
            "policy_cache_ttl_s": {
                "value": dsl_exit._POLICY_CACHE_TTL_S,
                "source": _env_then_cfg_source(
                    "dsl_state_io.policy_cache_ttl_s",
                    "HERMES_DSL_POLICY_CACHE_TTL_S", "f", None, allow_file=False),
            },
            "save_max_attempts": {
                "value": dsl_exit._SAVE_MAX_ATTEMPTS,
                "source": _env_then_cfg_source(
                    "dsl_state_io.save_max_attempts",
                    "HERMES_DSL_SAVE_MAX_ATTEMPTS", "i", None, allow_file=False),
            },
            "save_backoff_base_sec": {
                "value": dsl_exit._SAVE_BACKOFF_BASE_SEC,
                "source": _env_then_cfg_source(
                    "dsl_state_io.save_backoff_base_sec",
                    "HERMES_DSL_SAVE_BACKOFF_BASE_SEC", "f", None,
                    allow_file=False),
            },
            "save_backoff_factor": {
                "value": dsl_exit._SAVE_BACKOFF_FACTOR,
                "source": _cfg_layer_source(
                    "dsl_state_io.save_backoff_factor", None, allow_file=False),
            },
        }
    except Exception as e:
        view["dsl_state_io"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        from hermes_trader.agents import executor

        # fail_open arms ONLY on the exact env string "1"; any other env
        # value is inert and the canonical key (present in HERMES_CFG_ env or
        # the mounted file, even when False) decides the source.
        fail_open_source = (
            "env" if os.environ.get("HERMES_SPREAD_GATE_FAIL_OPEN", "0") == "1"
            else _cfg_layer_source("spread_gate_fail_open", raw)
        )
        view["executor"] = {
            "max_atr_pct": {
                "value": executor._resolve_max_atr_pct(),
                "source": _env_then_cfg_source(
                    "max_atr_pct", "HERMES_MAX_ATR_PCT", "f", raw),
            },
            "max_spread_pct": {
                "value": executor._resolve_max_spread_pct(),
                "source": _env_then_cfg_source(
                    "max_spread_pct", "HERMES_MAX_SPREAD_PCT", "f", raw),
            },
            "spread_gate_fail_open": {
                "value": executor._resolve_spread_gate_fail_open(),
                "source": fail_open_source,
            },
            "liq_buffer_usd": {
                "value": executor._resolve_liq_buffer_usd(),
                # Any float-coercible env wins, including "0" (gate disabled);
                # a non-numeric env falls through to the cfg layers.
                "source": _env_then_cfg_source(
                    "liq_buffer_usd", "HERMES_LIQ_BUFFER_USD", "f", raw),
            },
            "execution.taker_fee_pct": {
                "value": executor._resolve_hl_taker_fee_pct(),
                "source": _env_then_cfg_source(
                    "execution.taker_fee_pct", "HERMES_TAKER_FEE_PCT", "f", raw),
            },
        }
    except Exception as e:
        view["executor"] = {"error": f"{type(e).__name__}: {e}"}
    return view


def build_effective_config_snapshot() -> dict[str, Any]:
    """Resolve every canonical config key to (value, source) via the EXACT
    cfg_get provenance path (cfg_env/file/default), plus the legacy dedicated
    env switches and the P0-1 LIVE authorization flag. Pure read; never
    mutates state and never raises (returns an {"error": ...} leaf instead).

    P1-4 Phase 0 additions:
      * legacy_env_overrides now covers all FOUR gray-release MODE env vars
        (signal_age_decay was previously missing);
      * env_switches lists the dedicated kill/observability switches even
        when unset, alongside their default-when-unset (two default ON);
      * accessor_effective resolves loop_runtime knobs and the four gray
        modes through their real module accessors — the only place legacy
        env overrides of those paths are visible.
    """
    leaves: dict[str, dict[str, Any]] = {}
    for key in _iter_dotted_leaves(CANONICAL_DEFAULTS):
        try:
            value, source = _resolve_provenance(key)
        except Exception as e:  # snapshot must never break startup
            value, source = None, f"error:{type(e).__name__}"
        leaves[key] = {"value": value, "source": source}
    legacy_env = {
        k: os.environ.get(k) for k in _GRAY_MODE_ENV_KEYS
        if os.environ.get(k) is not None
    }
    env_switches = {
        name: {"env": os.environ.get(name), "default_when_unset": default}
        for name, default in _DEDICATED_ENV_SWITCHES
    }
    return {
        "generated_at_ms": int(time.time() * 1000),
        "config_file": CONFIG_PATH,
        "live_enabled": live_trading_authorized(),
        "legacy_env_overrides": legacy_env,
        "env_switches": env_switches,
        "accessor_effective": _accessor_effective_view(),
        "keys": leaves,
    }


def write_effective_config_snapshot(path: Optional[str] = None) -> Optional[str]:
    """Atomically persist the resolved-config snapshot and log every env
    override (generic HERMES_CFG_* and legacy) one per line. Best-effort:
    returns the written path, None when disabled, and never raises.
    """
    if path is None:
        path = _effective_config_snapshot_path()
    if not path:
        return None
    try:
        snapshot = build_effective_config_snapshot()
        atomic_io.write_json_atomic(
            path, snapshot, indent=2, fsync=False, ebusy_fallback=True)
        env_keys = sorted(
            k for k, v in snapshot["keys"].items() if v.get("source") == "cfg_env"
        )
        if env_keys:
            for k in env_keys:
                logger.warning(
                    "[config] ENV override in effect: %s=%r (via HERMES_CFG_%s)",
                    k, snapshot["keys"][k]["value"],
                    k.upper().replace(".", "__"))
        else:
            logger.info("[config] no HERMES_CFG_* env overrides in effect")
        if snapshot["legacy_env_overrides"]:
            for k, v in sorted(snapshot["legacy_env_overrides"].items()):
                logger.warning("[config] legacy ENV override in effect: %s=%r", k, v)
        logger.info(
            "[config] effective config snapshot written to %s "
            "(mode=%s, live_enabled=%s, %d keys)",
            path, snapshot["keys"].get("mode", {}).get("value"),
            snapshot["live_enabled"], len(snapshot["keys"]))
        return path
    except Exception as e:
        logger.error("[config] effective config snapshot failed: %s: %s",
                     type(e).__name__, e)
        return None


def _iter_dotted_leaves(node: dict[str, Any], prefix: str = ""):
    for k, v in node.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            yield from _iter_dotted_leaves(v, key)
        else:
            yield key


def _config_cli(argv: list[str]) -> int:
    import json as _json
    if len(argv) >= 2 and argv[1] == "explain" and len(argv) == 3:
        key = argv[2]
        env_name = "HERMES_CFG_" + key.upper().replace(".", "__")
        value, source = _resolve_provenance(key)
        print(f"key            : {key}")
        print(f"effective value: {value!r}")
        print(f"source         : {source}")
        print(f"cfg env name   : {env_name}")
        print(f"config file    : {CONFIG_PATH}")
        # cfg_get result is the source of truth for the running process.
        print(f"cfg_get()      : {cfg_get(key, '<no-default>')!r}")
        return 0
    if len(argv) == 2 and argv[1] == "--dump-effective":
        out: dict[str, dict[str, Any]] = {}
        for key in _iter_dotted_leaves(CANONICAL_DEFAULTS):
            value, source = _resolve_provenance(key)
            out[key] = {"value": value, "source": source}
        print(_json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("usage:\n"
          "  python -m hermes_trader.agents.config_store explain <dotted.key>\n"
          "  python -m hermes_trader.agents.config_store --dump-effective")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_config_cli(sys.argv))
