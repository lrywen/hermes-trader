"""Behavioral contract tests for executor SIZING-side pure functions.

T1 follow-up (architecture review 2026-09-11): executor.py (~6.2k LOC) had a
single direct test file. The DSL stop-width alignment (select_exit_params /
resolve_regime_clocks / compute_effective_stop_pct core_stop) is already
covered by test_f1_sync_stop_alignment / test_regime_exits /
test_e3_time_scratch_and_regime_clocks. These tests close the sizing branches
that had NO direct coverage, all of which sit on the live notional-sizing path
and are therefore fund-safety relevant:

  - conviction tier parsing (malformed -> safe defaults, non-positive
    multipliers dropped, highest-threshold-first ordering) and multiplier
    lookup (best tier the confidence clears; lowest tier as floor);
  - compute_effective_stop_pct's SIZING-ONLY adjustments: the ATR-spike
    breaker (tighten by 30% strictly beyond 2x mean, not at the boundary),
    exit-slip compensation (never narrows), and their composition — while
    core_stop must remain the pure three-layer value byte-aligned with DSL;
  - select_exit_params non_trend max-loss override (chop/neutral tight stop,
    directional up/down trend-ride widening).

Everything here is a pure dict/number function: no network, no global config,
no registry. Tests LOCK EXISTING BEHAVIOR and change no trading logic.
"""

import pytest

from hermes_trader.agents.executor import (
    _DEFAULT_CONVICTION_TIERS,
    _conviction_multiplier,
    _parse_conviction_tiers,
    compute_effective_stop_pct,
    select_exit_params,
)

# A minimal dsl config: regime_aware OFF, ROE cap disabled (100) so spot % is
# the exact effective stop. Mirrors the shape consumed on the live path.
DSL = {
    "protect_pct": 1.25,
    "retrace_threshold": 0.20,
    "max_loss_pct": 2.0,
    "max_loss_roe_pct": 100.0,
    "hard_timeout_minutes": 1e9,
    "stale_flat_timeout_minutes": 0.0,
}


# ── conviction tier parsing ─────────────────────────────────────────────────

def test_parse_tiers_sorts_highest_threshold_first():
    """Lookup relies on descending order; input may arrive unsorted."""
    tiers = _parse_conviction_tiers([[0.5, 2.0], [0.9, 3.0], [0.0, 0.5]])
    assert tiers == [(0.9, 3.0), (0.5, 2.0), (0.0, 0.5)]


def test_parse_tiers_drops_non_positive_multipliers():
    assert _parse_conviction_tiers([[0.5, 2.0], [0.6, 0.0], [0.7, -1.0]]) == \
        [(0.5, 2.0)]


@pytest.mark.parametrize("bad", [None, [], "", "x", 42, [["a", 1.0]],
                                [[0.5]], "not-a-list"])
def test_parse_tiers_falls_back_to_defaults_on_malformed(bad):
    assert _parse_conviction_tiers(bad) == _DEFAULT_CONVICTION_TIERS


def test_parse_tiers_all_non_positive_falls_back():
    assert _parse_conviction_tiers([[0.5, 0.0], [0.6, -2.0]]) == \
        _DEFAULT_CONVICTION_TIERS


# ── conviction multiplier lookup ────────────────────────────────────────────

def test_conviction_multiplier_picks_best_tier_cleared():
    tiers = [(0.9, 3.0), (0.5, 2.0), (0.0, 0.5)]
    assert _conviction_multiplier(0.95, tiers) == 3.0
    assert _conviction_multiplier(0.9, tiers) == 3.0    # boundary inclusive
    assert _conviction_multiplier(0.7, tiers) == 2.0
    assert _conviction_multiplier(0.5, tiers) == 2.0    # boundary inclusive
    assert _conviction_multiplier(0.1, tiers) == 0.5


def test_conviction_multiplier_lowest_tier_is_floor():
    # Confidence below every positive threshold still gets the lowest tier's
    # multiplier (never zero / never a negative notional).
    tiers = _DEFAULT_CONVICTION_TIERS
    assert _conviction_multiplier(0.0, tiers) == tiers[-1][1]
    assert _conviction_multiplier(-1.0, tiers) == tiers[-1][1]


# ── compute_effective_stop_pct: baseline + three-layer core ─────────────────

def test_baseline_effective_equals_spot_cap():
    bd = compute_effective_stop_pct(DSL, "neutral", leverage=1, atr_pct=0.0)
    assert bd["effective_stop_pct"] == pytest.approx(2.0)
    assert bd["core_stop"] == pytest.approx(2.0)
    assert bd["atr_spike"] is False
    assert bd["slip_adj_pct"] == 0.0


def test_roe_cap_binds_on_high_leverage():
    """max_loss_roe_pct/leverage must beat a loose spot cap at high lev
    (the 40x BTC case from the module docstring)."""
    dsl = dict(DSL, max_loss_pct=2.5, max_loss_roe_pct=50.0)
    bd = compute_effective_stop_pct(dsl, "neutral", leverage=40, atr_pct=0.0)
    assert bd["roe_cap"] == pytest.approx(1.25)
    assert bd["core_stop"] == pytest.approx(1.25)


# ── sizing-only ATR-spike breaker ───────────────────────────────────────────

def test_atr_spike_tightens_by_30pct_without_touching_core():
    # atr 3% > 2x mean 1% → breaker fires: effective = core*0.70 = 1.4.
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=3.0, atr_hist_mean_pct=1.0)
    assert bd["atr_spike"] is True
    assert bd["effective_stop_pct"] == pytest.approx(1.4)
    # core_stop is the un-tightened three-layer value (drift-guard anchor).
    assert bd["core_stop"] == pytest.approx(2.0)


def test_atr_spike_does_not_fire_at_or_below_2x_boundary():
    """Strictly greater-than 2x: atr == 2*mean must NOT tighten."""
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=2.0, atr_hist_mean_pct=1.0)
    assert bd["atr_spike"] is False
    assert bd["effective_stop_pct"] == pytest.approx(2.0)


def test_atr_spike_needs_historical_baseline():
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=5.0, atr_hist_mean_pct=0.0)
    assert bd["atr_spike"] is False


def test_atr_spike_can_be_disabled():
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=3.0, atr_hist_mean_pct=1.0,
        atr_spike_enabled=False)
    assert bd["atr_spike"] is False
    assert bd["effective_stop_pct"] == pytest.approx(2.0)


# ── sizing-only slippage compensation ───────────────────────────────────────

def test_slip_compensation_widens_never_narrows():
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=0.0, avg_exit_slip_pct=0.35)
    assert bd["slip_adj_pct"] == pytest.approx(0.35)
    assert bd["effective_stop_pct"] == pytest.approx(2.35)
    assert bd["core_stop"] == pytest.approx(2.0)  # core excludes slip


def test_negative_slip_is_clamped_to_zero():
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=0.0, avg_exit_slip_pct=-0.5)
    assert bd["slip_adj_pct"] == 0.0
    assert bd["effective_stop_pct"] == pytest.approx(2.0)


def test_spike_and_slip_compose():
    """Tighten by 30% first, then widen by slip: 2.0*0.70 + 0.35 = 1.75."""
    bd = compute_effective_stop_pct(
        DSL, "neutral", leverage=1, atr_pct=3.0, atr_hist_mean_pct=1.0,
        avg_exit_slip_pct=0.35)
    assert bd["atr_spike"] is True
    assert bd["slip_adj_pct"] == pytest.approx(0.35)
    assert bd["effective_stop_pct"] == pytest.approx(1.75)


# ── select_exit_params non_trend max-loss override ──────────────────────────

def _regime_aware_cfg():
    return dict(DSL, regime_aware={
        "enabled": True,
        "trend_ride": {"protect_pct": 3.0, "retrace_threshold": 0.55},
        "max_loss": {
            "trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0},
            "non_trend": {"max_loss_pct": 0.3, "max_loss_roe_pct": 4.0},
        },
    })


def test_non_trend_regimes_get_tight_overridden_stop():
    for regime in ("neutral", "chop"):
        _, _, _, ml, ml_roe, label = select_exit_params(_regime_aware_cfg(), regime)
        assert label == "scalp"
        assert ml == pytest.approx(0.3)
        assert ml_roe == pytest.approx(4.0)


def test_directional_regimes_get_trend_ride_wide_stop():
    for regime in ("up", "down"):
        prot, retrace, _, ml, ml_roe, label = select_exit_params(
            _regime_aware_cfg(), regime)
        assert label == f"trend_ride({regime}-regime)"
        assert prot == pytest.approx(3.0)
        assert retrace == pytest.approx(0.55)
        assert ml == pytest.approx(0.8)
        assert ml_roe == pytest.approx(10.0)


def test_non_trend_override_absent_falls_back_to_base_stop():
    # regime_aware enabled but no non_trend block → top-level dsl stop is kept.
    cfg = dict(DSL, regime_aware={"enabled": True, "max_loss": {}})
    _, _, _, ml, ml_roe, label = select_exit_params(cfg, "neutral")
    assert label == "scalp"
    assert ml == pytest.approx(2.0)
    assert ml_roe == pytest.approx(100.0)
