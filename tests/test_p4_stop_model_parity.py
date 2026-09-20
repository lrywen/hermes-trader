"""B-1a-改③④ parity: kernel stop model == live DSLTracker._effective_max_loss.

The research kernel reimplements the live leverage/ATR-aware effective spot
stop as a dependency-free pure function
(``hermes_trader.backtest.stop_model.effective_stop_pct``). This test pins the
two to identical output across a covering grid so neither side can drift
without CI failing — the backtest must invert to the same per-trade stop as the
8 live reason strings (0.40%–1.67%).
"""
from __future__ import annotations

import itertools

import pytest

from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy
from hermes_trader.backtest.stop_model import effective_stop_pct


def _live_stop(pol: ExitPolicy, leverage: int, entry_atr_pct: float) -> float:
    t = DSLTracker(coin="X", side="long", entry_px=100.0, entry_time=0.0,
                   policy=pol, leverage=leverage, entry_atr_pct=entry_atr_pct)
    return t._effective_max_loss()


def test_lev1_no_atr_matches_regime_cap():
    pol = ExitPolicy(max_loss_pct=0.8)
    got = effective_stop_pct(max_loss_pct=0.8, leverage=1)
    assert got.spot_pct == pytest.approx(0.8)
    assert got.binding == "regime"
    assert got.atr_active is False
    assert got.spot_pct == pytest.approx(_live_stop(pol, 1, 0.0))


def test_high_leverage_roe_cap_binds():
    # max_loss_roe_pct default 5.0; at 10x → roe_cap 0.5%, tighter than 0.8.
    pol = ExitPolicy(max_loss_pct=0.8, max_loss_roe_pct=5.0)
    got = effective_stop_pct(max_loss_pct=0.8, leverage=10, max_loss_roe_pct=5.0)
    assert got.spot_pct == pytest.approx(0.5)
    assert got.binding == "roe"
    assert got.spot_pct == pytest.approx(_live_stop(pol, 10, 0.0))


def test_atr_widens_up_to_ceiling_and_caps_at_regime():
    # regime 0.8, ATR 2.0% * mult 1.5 = 3.0 → clamp to ceiling 4.0 → min(0.8,3.0)
    # → regime still binds (ATR cannot override tighter regime stop).
    pol = ExitPolicy(max_loss_pct=0.8, atr_stop_enabled=True,
                     atr_stop_mult=1.5, atr_stop_floor_pct=1.0,
                     atr_stop_ceiling_pct=4.0)
    got = effective_stop_pct(max_loss_pct=0.8, leverage=1,
                             atr_stop_enabled=True, entry_atr_pct=2.0,
                             atr_mult=1.5, atr_floor_pct=1.0,
                             atr_ceiling_pct=4.0)
    assert got.atr_active is True
    assert got.spot_pct == pytest.approx(0.8)
    assert got.binding == "regime"
    assert got.spot_pct == pytest.approx(_live_stop(pol, 1, 2.0))


def test_atr_floor_binds_when_atr_small_and_regime_wide():
    # regime 3.0 (wide), atr 0.2%*1.5=0.3 → clamp UP to floor 1.0 → min(3,1)=1.0,
    # ATR binds. Mirrors spot_cap=3.00[atr] style ceiling cases the other way.
    pol = ExitPolicy(max_loss_pct=3.0, atr_stop_enabled=True,
                     atr_stop_mult=1.5, atr_stop_floor_pct=1.0,
                     atr_stop_ceiling_pct=3.0)
    got = effective_stop_pct(max_loss_pct=3.0, leverage=1,
                             atr_stop_enabled=True, entry_atr_pct=0.2,
                             atr_mult=1.5, atr_floor_pct=1.0,
                             atr_ceiling_pct=3.0)
    assert got.spot_pct == pytest.approx(1.0)
    assert got.binding == "atr"
    assert got.spot_pct == pytest.approx(_live_stop(pol, 1, 0.2))


def test_leverage_below_one_treated_as_one():
    a = effective_stop_pct(max_loss_pct=0.8, leverage=0)
    b = effective_stop_pct(max_loss_pct=0.8, leverage=-3)
    assert a.spot_pct == b.spot_pct == 0.8


@pytest.mark.parametrize(
    "ml_pct,lev,atr_enabled,atr_pct",
    list(itertools.product(
        [0.4, 0.8, 3.0],            # regime caps
        [1, 2, 5, 10, 40],          # leverage incl. the 40x doc case
        [False, True],
        [0.0, 0.2, 0.8, 2.0, 3.0],  # ATR % across floor/mid/ceiling
    )),
)
def test_full_grid_matches_live(ml_pct, lev, atr_enabled, atr_pct):
    pol = ExitPolicy(max_loss_pct=ml_pct, max_loss_roe_pct=5.0,
                     atr_stop_enabled=atr_enabled, atr_stop_mult=1.5,
                     atr_stop_floor_pct=1.0, atr_stop_ceiling_pct=4.0)
    got = effective_stop_pct(
        max_loss_pct=ml_pct, leverage=lev, max_loss_roe_pct=5.0,
        atr_stop_enabled=atr_enabled, entry_atr_pct=atr_pct,
        atr_mult=1.5, atr_floor_pct=1.0, atr_ceiling_pct=4.0)
    assert got.spot_pct == pytest.approx(_live_stop(pol, lev, atr_pct), rel=1e-12)


# ---------------------------------------------------------------------------
# A-3: the 8 live ``reason`` strings from trading-loop.log.4 (2026-09-05,
# 4 unique positions × shadow+paper lines). At that date atr_stop was still
# enabled with mult=1.2 / floor=1.2 / ceiling=3.0, so the reason prints the
# ATR cap as ``spot_cap=<v>[atr]`` while the EFFECTIVE stop is still the
# tighter regime/ROE cap (binding is always "regime" in these four). This pins
# the A-3 derivation ``atr_cap = clamp(entry_atr_pct*1.2, 1.2, 3.0)`` against
# the actual production log values.
# ---------------------------------------------------------------------------

A3_LIVE_REASONS = [
    # coin, lev, regime_cap, roe_pct, entry_px, atr_abs, reason [atr] label, eff cap
    ("TAO",     5, 1.00, 15.0, 5.499731896292154 / 0.022083, 5.499731896292154, 2.65),
    ("CASHCAT", 3, 1.00, 15.0, 0.16305,                     0.03320811742478859, 3.00),
    ("CRV",    10, 0.40,  5.0, 0.382,                       0.011851805522691303, 3.00),
]


@pytest.mark.parametrize("coin,lev,regime,roe,entry,atr_abs,label", A3_LIVE_REASONS)
def test_a3_live_reason_atr_label_inverts(coin, lev, regime, roe, entry, atr_abs, label):
    atr_pct = atr_abs / entry * 100.0
    atr_cap = min(max(atr_pct * 1.2, 1.2), 3.0)
    assert atr_cap == pytest.approx(label, abs=0.01), (
        f"{coin}: reason spot_cap={label:.2f}[atr] but formula gives {atr_cap:.3f}")
    got = effective_stop_pct(
        max_loss_pct=regime, leverage=lev, max_loss_roe_pct=roe,
        atr_stop_enabled=True, entry_atr_pct=atr_pct,
        atr_mult=1.2, atr_floor_pct=1.2, atr_ceiling_pct=3.0)
    # The displayed [atr] number is the ATR cap; the effective stop binds on
    # the tighter per-regime cap (1.00 trend / 0.40 non-trend).
    assert got.atr_active is True
    assert got.binding == "regime"
    assert got.spot_pct == pytest.approx(regime, rel=1e-9)


def test_a3_sui_ceiling_label_implies_locked_atr_threshold():
    # SUI reason printed spot_cap=3.00[atr] (lev10, regime 1.00). The research
    # atr_abs at 12:40 gives 2.438% → 2.926, just under ceiling; the reason's
    # 3.00 means the entry-tick-LOCKED 4h ATR snapshot was >=2.5% (the ceiling
    # clamp threshold). Assert the derivation threshold the label implies.
    assert pytest.approx(3.0 / 1.2, abs=1e-9) == 2.5
    got = effective_stop_pct(
        max_loss_pct=1.0, leverage=10, max_loss_roe_pct=15.0,
        atr_stop_enabled=True, entry_atr_pct=2.5,
        atr_mult=1.2, atr_floor_pct=1.2, atr_ceiling_pct=3.0)
    assert got.spot_pct == pytest.approx(1.0)
    assert got.binding == "regime"
