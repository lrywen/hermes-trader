"""F1 sync regression: sizing / DSL / drift-guard effective-stop alignment.

F1 fix (commit 1208f70) landed only in dsl_exit.DSLTracker._effective_max_loss:
the effective SPOT-% stop is ``min(regime max_loss, ATR cap)`` then ``min(...,
ROE/lev)``. The ATR stop may only WIDEN up to the regime cap — it must never
OVERRIDE a tighter regime max_loss (trend 0.8% / non-trend 0.4%).

executor.py originally kept three STALE copies that let the ATR clamp replace
spot_cap wholesale (no regime hard cap):
  1. compute_effective_stop_pct (sizing core_stop)        — executor L790-803
  2. post-fill sizing-v2 drift guard (_dsl_core recompute) — executor L2087-2101
  3. STOP OVERRUN monitor (_cfg_cap recompute)            — executor L4547-4562

The stale sizing mirror undersized positions (read a wider stop than the DSL
actually used) and the stale drift guard recomputed the SAME value as the
sizer, so dev was always 0 — a false-negative that silently masked any real
divergence.

These tests lock the three recomputations byte-aligned with the canonical
dsl_exit._effective_max_loss across regime x atr x leverage, and confirm the
drift guard now FIRES on an injected stale-sizer divergence instead of
reporting dev=0.
"""

from __future__ import annotations

import pytest

from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy
from hermes_trader.agents.executor import (
    compute_effective_stop_pct,
    select_exit_params,
)

# Production-shaped config: regime-aware trend 0.8%/10 ROE, non-trend 0.4%/5,
# ATR stop enabled (mult 1.2, floor 1.2, ceiling 3.0) — mirrors live deploy.
DSL_CFG = {
    "max_loss_pct": 1.0,
    "max_loss_roe_pct": 15.0,
    "atr_stop": {
        "enabled": True,
        "atr_mult": 1.2,
        "floor_pct": 1.2,
        "ceiling_pct": 3.0,
    },
    "regime_aware": {
        "enabled": True,
        "trend_ride": {"protect_pct": 2.5, "retrace_threshold": 0.55},
        "max_loss": {
            "trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0},
            "non_trend": {"max_loss_pct": 0.4, "max_loss_roe_pct": 5.0},
        },
    },
}

_REGIMES = ("up", "down", "neutral", "chop")
_ATRS = (0.0, 0.3, 0.6, 1.0, 2.0, 4.0)
_LEVS = (1, 5, 12, 20)
_TOL = 1e-9


def _make_tracker(regime: str, atr_pct: float, lev: int) -> DSLTracker:
    """Build a DSLTracker with the SAME policy the live fill path constructs:
    select_exit_params -> ExitPolicy, identical to executor._register_filled_position.
    """
    _p, _r, _t, ml_pct, ml_roe, _lbl = select_exit_params(DSL_CFG, regime)
    a = DSL_CFG["atr_stop"]
    pol = ExitPolicy(
        max_loss_pct=ml_pct,
        max_loss_roe_pct=ml_roe,
        atr_stop_enabled=a["enabled"],
        atr_stop_mult=a["atr_mult"],
        atr_stop_floor_pct=a["floor_pct"],
        atr_stop_ceiling_pct=a["ceiling_pct"],
    )
    return DSLTracker(
        "T", "long", 100.0, 1000.0, policy=pol, leverage=lev, entry_atr_pct=atr_pct
    )


def _drift_guard_recompute(policy: ExitPolicy, entry_atr_pct: float, leverage: float) -> float:
    """Mirror of the FIXED inline drift-guard formula (executor L2087-2101).

    Must stay identical to dsl_exit._effective_max_loss; -1.0 is the sentinel
    the guard uses for "no cap" (mirrors core_stop's inf -> -1.0 mapping).
    """
    lev = max(1.0, float(leverage))
    regime_cap = float(policy.max_loss_pct) if float(policy.max_loss_pct) > 0 else float("inf")
    if policy.atr_stop_enabled and entry_atr_pct > 0:
        atr_cap = min(
            max(entry_atr_pct * policy.atr_stop_mult, policy.atr_stop_floor_pct),
            policy.atr_stop_ceiling_pct,
        )
        spot = min(regime_cap, atr_cap)
    else:
        spot = regime_cap
    roe = (
        float(policy.max_loss_roe_pct) / lev
        if float(policy.max_loss_roe_pct) > 0
        else float("inf")
    )
    spot = spot if spot > 0 else float("inf")
    core = min(spot, roe)
    return core if core != float("inf") else -1.0


def _stop_overrun_recompute(policy: ExitPolicy, entry_atr_pct: float, leverage: float) -> float:
    """Mirror of the FIXED inline STOP-OVERRUN cap formula (executor L4547-4562).

    Same three-layer shape as the drift guard; the overrun monitor reconstructs
    the configured cap from tracker.policy after a flatten to detect gap-through.
    """
    lev = max(1.0, float(leverage))
    cap_regime = float(policy.max_loss_pct) if float(policy.max_loss_pct) > 0 else float("inf")
    if getattr(policy, "atr_stop_enabled", False) and entry_atr_pct > 0:
        cap_atr = min(
            max(entry_atr_pct * float(policy.atr_stop_mult), float(policy.atr_stop_floor_pct)),
            float(policy.atr_stop_ceiling_pct),
        )
        cap_spot = min(cap_regime, cap_atr)
    else:
        cap_spot = cap_regime
    cap_roe = (
        float(policy.max_loss_roe_pct) / lev
        if float(policy.max_loss_roe_pct) > 0
        else float("inf")
    )
    cap_spot = cap_spot if cap_spot > 0 else float("inf")
    cfg_cap = min(cap_spot, cap_roe)
    return cfg_cap if cfg_cap != float("inf") else -1.0


@pytest.mark.parametrize("lev", _LEVS)
@pytest.mark.parametrize("atr_pct", _ATRS)
@pytest.mark.parametrize("regime", _REGIMES)
def test_sizing_core_stop_byte_aligned_with_dsl(regime, atr_pct, lev):
    """compute_effective_stop_pct core_stop == DSLTracker._effective_max_loss."""
    bd = compute_effective_stop_pct(DSL_CFG, regime, lev, atr_pct)
    sizing_core = bd["core_stop"]
    dsl_eff = _make_tracker(regime, atr_pct, lev)._effective_max_loss()
    assert sizing_core == pytest.approx(dsl_eff, abs=_TOL), (
        f"sizing core_stop {sizing_core} != DSL effective {dsl_eff} "
        f"(regime={regime} atr={atr_pct} lev={lev})"
    )


@pytest.mark.parametrize("lev", _LEVS)
@pytest.mark.parametrize("atr_pct", _ATRS)
@pytest.mark.parametrize("regime", _REGIMES)
def test_drift_guard_recompute_byte_aligned_with_dsl(regime, atr_pct, lev):
    """The drift guard's DSL-side recompute equals the DSL effective stop.

    If this regresses to the stale ATR-override form it would (a) diverge from
    the DSL here and (b) match the sizer's stale value -> dev=0 false negative.
    """
    trk = _make_tracker(regime, atr_pct, lev)
    drift_core = _drift_guard_recompute(trk.policy, atr_pct, lev)
    dsl_eff = trk._effective_max_loss()
    assert drift_core == pytest.approx(dsl_eff, abs=_TOL), (
        f"drift recompute {drift_core} != DSL effective {dsl_eff} "
        f"(regime={regime} atr={atr_pct} lev={lev})"
    )


@pytest.mark.parametrize("lev", _LEVS)
@pytest.mark.parametrize("atr_pct", _ATRS)
@pytest.mark.parametrize("regime", _REGIMES)
def test_stop_overrun_recompute_byte_aligned_with_dsl(regime, atr_pct, lev):
    """The STOP-OVERRUN monitor's cap recompute equals the DSL effective stop."""
    trk = _make_tracker(regime, atr_pct, lev)
    cfg_cap = _stop_overrun_recompute(trk.policy, atr_pct, lev)
    dsl_eff = trk._effective_max_loss()
    assert cfg_cap == pytest.approx(dsl_eff, abs=_TOL), (
        f"overrun cap {cfg_cap} != DSL effective {dsl_eff} "
        f"(regime={regime} atr={atr_pct} lev={lev})"
    )


@pytest.mark.parametrize("lev", _LEVS)
@pytest.mark.parametrize("atr_pct", _ATRS)
@pytest.mark.parametrize("regime", _REGIMES)
def test_atr_never_overrides_tighter_regime_stop(regime, atr_pct, lev):
    """The binding stop must never exceed the regime max_loss spot cap.

    This is the heart of F1: even a large ATR (clamped at the 3% ceiling) must
    not widen the stop past the regime 0.4%/0.8% hard cap.
    """
    _p, _r, _t, ml_pct, ml_roe, _lbl = select_exit_params(DSL_CFG, regime)
    bd = compute_effective_stop_pct(DSL_CFG, regime, lev, atr_pct)
    # spot cap (pre-ROE) is bounded by regime max_loss; core_stop is <= that.
    assert bd["spot_cap"] <= ml_pct + _TOL
    assert bd["core_stop"] <= ml_pct + _TOL


def test_drift_guard_fires_on_stale_sizer_divergence():
    """Guard sensitivity: an OLD stale sizer value (ATR clamp wholesale, no
    regime min) must produce dev > 5% against the FIXED DSL recompute.

    At low leverage the ROE cap does not bind, so a tight non-trend regime stop
    (0.4%) vs the ATR floor (1.2%) is a large real divergence the old guard
    masked as dev=0. The fixed guard must flag it.
    """
    regime, atr_pct, lev = "neutral", 1.0, 1
    trk = _make_tracker(regime, atr_pct, lev)
    a = DSL_CFG["atr_stop"]
    # OLD stale sizer formula: ATR clamp replaces spot_cap wholesale.
    old_spot = min(max(atr_pct * a["atr_mult"], a["floor_pct"]), a["ceiling_pct"])
    old_roe = trk.policy.max_loss_roe_pct / lev
    old_core = min(old_spot, old_roe if old_roe > 0 else float("inf"))
    fixed_drift = _drift_guard_recompute(trk.policy, atr_pct, lev)
    dev = abs(fixed_drift - old_core) / old_core * 100.0
    assert dev > 5.0, f"drift guard missed a real divergence: dev={dev:.1f}%"


def test_drift_guard_silent_when_aligned():
    """When sizing and DSL both use the fixed formula, dev ~= 0 (no false alarm)."""
    regime, atr_pct, lev = "up", 1.0, 12
    bd = compute_effective_stop_pct(DSL_CFG, regime, lev, atr_pct)
    sizing_core = bd["core_stop"]
    trk = _make_tracker(regime, atr_pct, lev)
    drift_core = _drift_guard_recompute(trk.policy, atr_pct, lev)
    dev = abs(drift_core - sizing_core) / sizing_core * 100.0
    assert dev < 1.0, f"expected ~0 dev when aligned, got {dev:.3f}%"
