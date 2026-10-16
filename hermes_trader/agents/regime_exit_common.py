"""Single regime-exit overlay implementation (audit 2026-10-15 P3).

The audit found the "overlay regime exit params onto a base policy" routine
hand-copied in three places:

  * ``dsl_exit._regime_aware_policy_for``   (synthesised-tracker policy)
  * ``shadow_book._build_policy``            (paper/backtest-stop parity)
  * ``backtest/regime.regime_aware_policy``  (backtest replayer)

All three delegate to the same production sources (``executor.select_exit_params``
+ ``executor.resolve_regime_clocks``) and all apply the SAME six-field overlay
(max_loss_pct / max_loss_roe_pct / protect_pct / retrace_threshold /
hard_timeout_minutes / stale_flat_timeout_minutes / phase2_tiers), but each
re-copied the RetraceTier conversion and the field list. A future drift of
that list (e.g. adding a new regime arm) would silently diverge the three —
``backtest/regime.py`` already records one such near-miss (dict->RetraceTier).

This module is the single implementation of "overlay the regime-split exit
params onto a base ExitPolicy". Callers still own:

  * where the base policy comes from (config policy vs an externally supplied
    one), and
  * the fail-open behaviour (what to return if resolution throws).

The RetraceTier conversion is preserved here as a hard invariant: raw dict
tiers from ``select_exit_params`` MUST become ``RetraceTier`` objects before
being placed in ``phase2_tiers``, else ``dsl_exit`` fails reading
``tier.pct_above_entry``.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from hermes_trader.agents.dsl_exit import ExitPolicy


def overlay_regime_exit(base: ExitPolicy, dsl_config: dict[str, Any],
                        regime: str = "neutral") -> tuple[ExitPolicy, str]:
    """Return ``(policy, label)`` with the regime-split exit params overlaid.

    Delegates 100% to production sources: ``select_exit_params`` for the
    trend/scalp stops and ``resolve_regime_clocks`` for the lifetime clocks.
    ``base`` supplies every OTHER knob (noise_band / smooth / scratch / confirm
    gates ...). The six regime-owned fields are replaced and the raw dict
    tiers are converted to ``RetraceTier`` (never left as raw dicts). Empty
    tiers fall back to the base policy's own tiers.
    """
    from hermes_trader.agents.executor import (
        resolve_regime_clocks,
        select_exit_params,
    )

    protect, retrace, tiers_raw, ml_pct, ml_roe, label = \
        select_exit_params(dsl_config, regime)
    # P3 invariant (audit 2026-10-15; see backtest/regime.py near-miss): raw
    # dict tiers MUST become RetraceTier objects, never placed as raw dicts.
    if tiers_raw:
        from hermes_trader.agents.dsl_exit import RetraceTier
        tiers = [RetraceTier(**t) for t in tiers_raw]
    else:
        tiers = list(base.phase2_tiers)
    clocks = resolve_regime_clocks(dsl_config, regime)
    policy = dataclasses.replace(
        base,
        max_loss_pct=float(ml_pct),
        max_loss_roe_pct=float(ml_roe),
        protect_pct=float(protect),
        retrace_threshold=float(retrace),
        hard_timeout_minutes=float(clocks["hard_timeout_minutes"]),
        stale_flat_timeout_minutes=float(clocks["stale_flat_timeout_minutes"]),
        phase2_tiers=tiers,
    )
    return policy, label
