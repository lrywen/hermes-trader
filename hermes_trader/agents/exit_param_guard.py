"""Regime-aware exit parameter shadowing guard (audit 2026-10-15 P0-3).

The DSL exit layer is dual-track: a set of top-level ``dsl_exit.*`` scalars
(protect / retrace / max_loss / max_loss_roe / hard_timeout / stale_flat) and
a nested ``regime_aware`` block that, when enabled, replaces them per regime.
The two tracks drift silently — an operator tuning the top-level
``dsl_exit.max_loss_pct`` reasonably assumes it changes the live stop, but
under ``regime_aware.enabled=true`` the directional exit never reads it, and
under ``clocks.enabled=true`` the lifetime clocks come from the regime
buckets too. There is no single place naming which top-level key is live.

This module does NOT decide the values or remove keys. It mirrors the exact
selection precedence in :func:`executor.select_exit_params` /
:func:`executor.resolve_regime_clocks` and reports, for each top-level exit
scalar, the regimes under which it is **shadowed** (a nested value wins) vs
still **reachable** (the nested key is absent and the top-level is the
fallback). Findings are surfaced as startup WARNINGs — never raised — so the
dual track stays visible without blocking a safety-critical boot.

Reachability encoded (must stay byte-aligned with those two functions):

  trend regime (up/down), regime_aware.enabled:
    protect_pct/retrace_threshold/phase2_tiers  <- regime_aware.trend_ride.*
    max_loss_pct/max_loss_roe_pct               <- regime_aware.max_loss.trend.*
  non-trend (neutral/chop):
    protect_pct/retrace_threshold/phase2_tiers  <- TOP-LEVEL (trend_ride is
                                                   directional only)
    max_loss_pct/max_loss_roe_pct               <- max_loss.non_trend.* if
                                                   present, else top-level
  clocks (regime_aware.clocks.enabled):
    hard_timeout/stale_flat                     <- clocks.<bucket>.* if present,
                                                   else top-level
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# top-level scalar -> (nested block path, nested key)
_TREND_RIDE_KEYS = {
    "protect_pct": ("trend_ride", "protect_pct"),
    "retrace_threshold": ("trend_ride", "retrace_threshold"),
    "phase2_tiers": ("trend_ride", "phase2_tiers"),
}
_MAX_LOSS_KEYS = ("max_loss_pct", "max_loss_roe_pct")
_CLOCK_KEYS = ("hard_timeout_minutes", "stale_flat_timeout_minutes")
_TREND_REGIMES = ("up", "down")
_NON_TREND_REGIMES = ("neutral", "chop")


def _present(mapping: Any, key: str) -> bool:
    return isinstance(mapping, Mapping) and key in mapping and mapping[key] is not None


def exit_shadow_findings(config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return per-key ``{key, top_level, shadowed_in, reachable_in}`` findings.

    Only reports keys that are actually shadowed for at least one regime under
    the *currently enabled* tracks. When regime_aware (and clocks) are disabled
    every top-level key is live and the list is empty.
    """
    dsl = config.get("dsl_exit")
    if not isinstance(dsl, Mapping):
        return []
    ra = dsl.get("regime_aware")
    ra_enabled = bool(isinstance(ra, Mapping) and ra.get("enabled", False))
    clocks = ra.get("clocks") if isinstance(ra, Mapping) else None
    clocks_enabled = bool(isinstance(clocks, Mapping) and clocks.get("enabled", False))

    if not ra_enabled and not clocks_enabled:
        return []

    trend_ride = ra.get("trend_ride") if isinstance(ra, Mapping) else None
    max_loss = ra.get("max_loss") if isinstance(ra, Mapping) else None
    trend_ml = max_loss.get("trend") if isinstance(max_loss, Mapping) else None
    nontrend_ml = max_loss.get("non_trend") if isinstance(max_loss, Mapping) else None
    trend_clk = clocks.get("trend") if isinstance(clocks, Mapping) else None
    nontrend_clk = clocks.get("non_trend") if isinstance(clocks, Mapping) else None

    findings: list[dict[str, str]] = []

    def _add(key: str, shadowed: list[str], reachable: list[str]) -> None:
        if not shadowed:
            return
        findings.append({
            "key": f"dsl_exit.{key}",
            "top_level": _describe(dsl.get(key)),
            "shadowed_in": ", ".join(shadowed),
            "reachable_in": ", ".join(reachable) if reachable else "nowhere",
        })

    if ra_enabled:
        # trend_ride block is directional only: it shadows top-level in
        # up/down; non-trend always falls back to the top-level scalars.
        for key, (_block, nested) in _TREND_RIDE_KEYS.items():
            if _present(trend_ride, nested):
                _add(key, list(_TREND_REGIMES), list(_NON_TREND_REGIMES))
        # max_loss.*: trend block shadows up/down; non_trend block shadows
        # neutral/chop only when that bucket actually carries the key, else
        # those regimes fall back to the top-level value.
        for key in _MAX_LOSS_KEYS:
            shadowed: list[str] = []
            reachable: list[str] = []
            if _present(trend_ml, key):
                shadowed.extend(_TREND_REGIMES)
            else:
                reachable.extend(_TREND_REGIMES)
            if _present(nontrend_ml, key):
                shadowed.extend(_NON_TREND_REGIMES)
            else:
                reachable.extend(_NON_TREND_REGIMES)
            _add(key, shadowed, reachable)

    if clocks_enabled:
        for key in _CLOCK_KEYS:
            shadowed = []
            reachable = []
            if _present(trend_clk, key):
                shadowed.extend(_TREND_REGIMES)
            else:
                reachable.extend(_TREND_REGIMES)
            if _present(nontrend_clk, key):
                shadowed.extend(_NON_TREND_REGIMES)
            else:
                reachable.extend(_NON_TREND_REGIMES)
            _add(key, shadowed, reachable)

    return findings


def _describe(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, dict)):
        return "<set>"
    return str(value)


def exit_shadow_warnings(config: Mapping[str, Any]) -> list[str]:
    """Human-readable startup warnings for shadowed top-level exit keys."""
    out: list[str] = []
    for f in exit_shadow_findings(config):
        out.append(
            f"exit dual-track: top-level {f['key']}={f['top_level']} is SHADOWED "
            f"in regime [{f['shadowed_in']}] by the regime_aware block (still "
            f"reachable in [{f['reachable_in']}]); tuning the top-level key has "
            "no effect in the shadowed regimes — edit the nested regime value")
    return out


# ── Backup-stop-width drift guard (audit 2026-10-15 P1, SL width) ──────────
#
# Stop width is NOT a duplicated implementation to collapse:
#   * ``dsl_exit.atr_stop.{atr_mult,floor_pct,ceiling_pct}`` is the CANONICAL
#     stop-width block both layers resolve through (_shared_atr_stop_config,
#     compute_backup_sl_width_pct — auto executor, manual server.py and
#     shadow_book). Even with atr_stop.enabled=False (409-trade study: "no edge
#     on/off" for the DSL's OWN ATR width), these keys stay live because they
#     size the EXCHANGE BACKUP disaster-net. They must never be deleted.
#   * top-level ``sl_atr_mult`` / ``sl_floor_pct`` are deliberate BACKUP-LAYER
#     overrides (precedence: explicit sl_* > shared atr_stop > module default).
#   * the two ceilings are INTENTIONALLY different (DSL 3% vs backup outer-net
#     clamp) — that divergence is by design and is never warned.
#
# The real residual risk is the operator setting the same number in both places
# and later retuning only one (the HYPE-class gap). This guard warns when an
# EXPLICIT top-level sl_* multiplier/floor differs from the shared block. It
# does not fire when the top-level key is absent (then the shared block is the
# sole source, which is the desired state).

_STOP_WIDTH_DRIFT_EPS = 1e-9


def stop_width_drift_findings(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return explicit backup-layer stop-width overrides that diverge from the
    shared ``dsl_exit.atr_stop`` canonical block. Empty when the top-level keys
    are absent (shared block is the single source) or equal."""
    if not isinstance(config, Mapping):
        return []
    dsl = config.get("dsl_exit")
    shared = dsl.get("atr_stop") if isinstance(dsl, Mapping) else None
    if not isinstance(shared, Mapping):
        return []

    def _num(v: Any) -> float | None:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if x == x else None  # NaN guard

    pairs = (
        ("sl_atr_mult", "atr_mult", "backup-SL ATR multiplier"),
        ("sl_floor_pct", "floor_pct", "backup-SL floor %"),
    )
    findings: list[dict[str, Any]] = []
    for top_key, shared_key, label in pairs:
        if top_key not in config:
            continue  # absent -> shared block is the single source; no drift
        top_v = _num(config.get(top_key))
        shared_v = _num(shared.get(shared_key))
        if top_v is None or shared_v is None:
            continue
        if abs(top_v - shared_v) > _STOP_WIDTH_DRIFT_EPS:
            findings.append({
                "top_key": top_key,
                "shared_key": f"dsl_exit.atr_stop.{shared_key}",
                "top_value": top_v,
                "shared_value": shared_v,
                "label": label,
            })
    return findings


def stop_width_drift_warnings(config: Mapping[str, Any]) -> list[str]:
    """Human-readable startup warnings for stop-width override divergence."""
    return [
        f"stop-width drift: top-level {f['top_key']}={f['top_value']:g} overrides "
        f"the shared {f['shared_key']}={f['shared_value']:g} for the "
        f"{f['label']}; the two stop layers ({f['top_key']} vs the shared block) "
        "would size the exchange backup net differently — align them or remove "
        f"the top-level {f['top_key']} to use the single source. (The separate "
        "backup/DSL ceilings are intentionally different and are not checked.)"
        for f in stop_width_drift_findings(config)
    ]
