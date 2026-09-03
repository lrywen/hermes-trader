"""Trading-loop startup/runtime knob resolution (P1-6).

The eighteen ``HERMES_*`` knobs that tune ``scripts/trading_loop.py`` used to
be read inline via ``os.environ.get(...)`` at module load, so they never
appeared in ``CANONICAL_DEFAULTS`` — invisible to the dashboard config dump
and ``validate_config_updates``, and unreachable through the canonical
``HERMES_CFG_<BLOCK>__<KEY>`` env / agent-config channel. They now resolve
through this importable helper, deliberately kept OUT of
``scripts/trading_loop.py`` (which runs module-level startup side effects and
a ``while True`` and therefore cannot be imported) so the resolution chain is
unit-testable, mirroring ``realtime_feed`` / ``perception.scan_budget_params``.

Resolution per leaf (highest → lowest priority):
  1. Legacy ``HERMES_*`` env var (operator / compose / k8s-configmap knobs —
     these keep working and take precedence, identical to the old inline read).
  2. ``cfg_get("loop_runtime.<leaf>")`` — covers ``HERMES_CFG_LOOP_RUNTIME__*``
     env, the agent-config dict and ``CANONICAL_DEFAULTS``.
  3. The inline literal default.

Defaults mirror the historical ``scripts/trading_loop.py`` literals verbatim;
this is a startup-tuning block, not a strategy block — no trading semantics
live here. Any coercion failure falls back to the literal so the loop never
crashes reading its own config.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get

logger = logging.getLogger("hermes-loop-runtime")

BLOCK = "loop_runtime"

# Truth set kept byte-for-byte identical to the inline reads the loop used
# (``os.environ.get(...).lower() in ('1', 'true', 'yes', 'on')``).
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})

# leaf → (legacy env var or None, kind: "str" | "bool" | "int" | "float").
# Order mirrors CANONICAL_DEFAULTS["loop_runtime"]; the resolver returns leaves
# keyed by the same snake_case names.
_LEGACY_ENV_SPEC: dict[str, tuple[Optional[str], str]] = {
    "loop_log_path": ("HERMES_LOOP_LOG_FILE", "str"),
    "surge_min_score": ("HERMES_SURGE_MIN_SCORE", "float"),
    "watchdog_timeout_s": ("HERMES_WATCHDOG_TIMEOUT_S", "int"),
    "exit_checkpoint_min_interval_s": ("HERMES_EXIT_CHECKPOINT_MIN_INTERVAL_S", "float"),
    "meta_prewarm_timeout_s": ("HERMES_META_PREWARM_TIMEOUT_S", "float"),
    "universe_refresh_s": ("HERMES_UNIVERSE_REFRESH_S", "int"),
    "startup_grace_s": ("HERMES_STARTUP_GRACE_S", "float"),
    "scan_interval": ("HERMES_SCAN_INTERVAL", "int"),
    "scan_dynamic": ("HERMES_SCAN_DYNAMIC", "bool"),
    "scan_fresh_s": ("HERMES_SCAN_FRESH_S", "float"),
    "scan_interval_fast": ("HERMES_SCAN_INTERVAL_FAST", "int"),
    "scan_interval_slow": ("HERMES_SCAN_INTERVAL_SLOW", "int"),
    "ws_fill_wake": ("HERMES_WS_FILL_WAKE", "bool"),
    "ws_status_event": ("HERMES_WS_STATUS_EVENT", "bool"),
    "ws_status_hold_s": ("HERMES_WS_STATUS_HOLD_S", "float"),
    "ws_status_fresh_s": ("HERMES_WS_STATUS_FRESH_S", "float"),
    "research_parallel": ("HERMES_RESEARCH_PARALLEL", "bool"),
    "research_parallel_workers": ("HERMES_RESEARCH_PARALLEL_WORKERS", "int"),
}

# Inline-literal defaults — the exact values trading_loop.py used in its
# os.environ.get calls. CANONICAL_DEFAULTS["loop_runtime"] mirrors these; the
# copy here is the resolver's last-resort fallback and is asserted equal by the
# drift-sentinel test.
LOOP_RUNTIME_DEFAULTS: dict[str, Any] = {
    "loop_log_path": "/data/trading-loop.log",
    "surge_min_score": 40.0,
    "watchdog_timeout_s": 600,
    "exit_checkpoint_min_interval_s": 5.0,
    "meta_prewarm_timeout_s": 3.0,
    "universe_refresh_s": 1800,
    "startup_grace_s": 12.0,
    "scan_interval": 15,
    "scan_dynamic": False,
    "scan_fresh_s": 10.0,
    "scan_interval_fast": 8,
    "scan_interval_slow": 20,
    "ws_fill_wake": False,
    "ws_status_event": False,
    "ws_status_hold_s": 30.0,
    "ws_status_fresh_s": 10.0,
    "research_parallel": True,
    "research_parallel_workers": 4,
}


def _coerce_leaf(raw: Any, kind: str) -> Any:
    """Coerce a raw env/config value to *kind*. Raises on failure (caller
    falls back to the literal). Bools accept the historical truth-set for
    strings and pass real bools through."""
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return bool(raw)
        return str(raw).strip().lower() in _TRUE_TOKENS
    if kind == "int":
        # Reject bool (bool is an int subclass) — a True/False config value
        # must not silently become 1/0.
        if isinstance(raw, bool):
            raise ValueError("bool is not an int leaf")
        return int(raw)
    if kind == "float":
        if isinstance(raw, bool):
            raise ValueError("bool is not a float leaf")
        return float(raw)
    return str(raw)


def loop_runtime_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the eighteen trading-loop runtime knobs as an independent dict.

    Per leaf: legacy ``HERMES_*`` env (highest priority) →
    ``cfg_get("loop_runtime.<leaf>")`` (HERMES_CFG_LOOP_RUNTIME__* env +
    agent-config + CANONICAL_DEFAULTS) → the inline literal. An empty-string
    env value is treated as unset (the old ``int("")`` would have raised).
    Returns a fresh dict each call; on any failure the whole result falls back
    to the literal defaults so the loop's startup never raises.
    """
    p = dict(LOOP_RUNTIME_DEFAULTS)
    try:
        for leaf, (legacy_env, kind) in _LEGACY_ENV_SPEC.items():
            raw: Any = None
            if legacy_env is not None:
                raw = os.environ.get(legacy_env)
            if raw is None or raw == "":
                raw = cfg_get(f"{BLOCK}.{leaf}", config=config)
            if raw is None:
                continue
            p[leaf] = _coerce_leaf(raw, kind)
    except Exception as e:  # pragma: no cover - defensive: never crash startup
        logger.warning(f"[loop_runtime] params read failed, using literals: {e}")
        return dict(LOOP_RUNTIME_DEFAULTS)
    return p


def _canonical_matches_literals() -> bool:
    """Internal consistency check (used by tests): CANONICAL_DEFAULTS block
    must mirror the inline-literal defaults exactly, leaf-for-leaf and
    value-for-value (including int-vs-float type)."""
    block = CANONICAL_DEFAULTS.get(BLOCK)
    if not isinstance(block, dict) or set(block) != set(LOOP_RUNTIME_DEFAULTS):
        return False
    for leaf, default in LOOP_RUNTIME_DEFAULTS.items():
        if block[leaf] != default or type(block[leaf]) is not type(default):
            return False
    return True
