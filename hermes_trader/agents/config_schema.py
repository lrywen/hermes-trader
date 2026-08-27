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
from typing import Any, Dict, List, get_origin

from pydantic import BaseModel, ConfigDict, Field

from hermes_trader.agents.config_store import CANONICAL_DEFAULTS


def _dict_default(key: str) -> Dict[str, Any]:
    return deepcopy(CANONICAL_DEFAULTS[key])


def _list_default(key: str) -> List[Any]:
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

    # ── scalars: ints ─────────────────────────────────────────────────────
    leverage: int = Field(default=CANONICAL_DEFAULTS["leverage"], ge=1, le=50)
    max_concurrent: int = Field(default=CANONICAL_DEFAULTS["max_concurrent"], ge=0)
    cooldown_min: int = Field(default=CANONICAL_DEFAULTS["cooldown_min"], ge=0, le=100_000)
    max_crypto_long_correlated: int = Field(default=CANONICAL_DEFAULTS["max_crypto_long_correlated"], ge=0, le=50)
    loss_cooldown_min: int = Field(default=CANONICAL_DEFAULTS["loss_cooldown_min"], ge=0, le=100_000)
    min_ai_close_hold_min: int = Field(default=CANONICAL_DEFAULTS["min_ai_close_hold_min"], ge=0, le=100_000)
    funding_lookback_hours: int = Field(default=CANONICAL_DEFAULTS["funding_lookback_hours"], ge=1, le=720)
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
    min_trend_score: float = Field(default=CANONICAL_DEFAULTS["min_trend_score"], ge=0.0, le=1.0)
    whale_size_multiplier: float = Field(default=CANONICAL_DEFAULTS["whale_size_multiplier"], ge=0.0)

    # ── lists (accepted as-is, element type not checked) ──────────────────
    coin_allowlist: list = Field(default_factory=lambda: _list_default("coin_allowlist"))
    coin_blocklist: list = Field(default_factory=lambda: _list_default("coin_blocklist"))
    hip3_dex_allowlist: list = Field(default_factory=lambda: _list_default("hip3_dex_allowlist"))
    hip3_dex_blocklist: list = Field(default_factory=lambda: _list_default("hip3_dex_blocklist"))

    # ── nested objects (accepted as dicts, not deep-validated) ────────────
    dsl_exit: Dict[str, Any] = Field(default_factory=lambda: _dict_default("dsl_exit"))
    runner_entry_gate: Dict[str, Any] = Field(default_factory=lambda: _dict_default("runner_entry_gate"))
    plan_b: Dict[str, Any] = Field(default_factory=lambda: _dict_default("plan_b"))
    atr_risk_sizing: Dict[str, Any] = Field(default_factory=lambda: _dict_default("atr_risk_sizing"))
    regime_classifier: Dict[str, Any] = Field(default_factory=lambda: _dict_default("regime_classifier"))
    debate_gate: Dict[str, Any] = Field(default_factory=lambda: _dict_default("debate_gate"))
    debate_research: Dict[str, Any] = Field(default_factory=lambda: _dict_default("debate_research"))
    signal_enforcement: Dict[str, Any] = Field(default_factory=lambda: _dict_default("signal_enforcement"))
    momentum_continuation: Dict[str, Any] = Field(default_factory=lambda: _dict_default("momentum_continuation"))
    candlestick_patterns: Dict[str, Any] = Field(default_factory=lambda: _dict_default("candlestick_patterns"))
    capital_rotation: Dict[str, Any] = Field(default_factory=lambda: _dict_default("capital_rotation"))
    gex_signal: Dict[str, Any] = Field(default_factory=lambda: _dict_default("gex_signal"))
    shadow_signals: Dict[str, Any] = Field(default_factory=lambda: _dict_default("shadow_signals"))
    momentum_reentry: Dict[str, Any] = Field(default_factory=lambda: _dict_default("momentum_reentry"))
    runner_mover_surface: Dict[str, Any] = Field(default_factory=lambda: _dict_default("runner_mover_surface"))
    memory_limits: Dict[str, Any] = Field(default_factory=lambda: _dict_default("memory_limits"))
    llm_circuit_breaker: Dict[str, Any] = Field(default_factory=lambda: _dict_default("llm_circuit_breaker"))
    coin_overrides: Dict[str, Any] = Field(default_factory=lambda: _dict_default("coin_overrides"))


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


def validate_config_updates(updates: Dict[str, Any], *, strict_keys: bool = True) -> List[str]:
    """Validate a partial config update. Returns a list of error strings.

    ``strict_keys=True`` (web API, terminal ``set``, CLI): unknown keys are
    rejected. ``strict_keys=False`` (legacy ``POST /api/agent/config``):
    unknown keys are left for the deep-merge path to persist.
    """
    errors: List[str] = []
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
    return errors
