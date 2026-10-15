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
