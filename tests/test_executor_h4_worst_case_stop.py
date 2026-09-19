"""Characterization tests for the H4 worst-case stop-distance estimate leaf.

_h4_worst_case_stop_pct mirrors the post-fill _place_backup_sl clamp so the
pre-trade H4 estimate (which feeds liquidation_buffer_gate) matches what the
backup SL actually clamps to. These tests pin the [floor, ceiling] width clamp
and the 0.5x-ceiling slip widen (capped at 1.5x ceiling) after the verbatim
extraction from maybe_execute.
"""

import pytest

from hermes_trader.agents.executor import _h4_worst_case_stop_pct


def test_raw_width_between_floor_and_ceiling_adds_half_ceiling_slip():
    # raw = (1 / 100) * 5 * 100 = 5.0 -> clamped to ceiling 3.0, then +1.5.
    pct = _h4_worst_case_stop_pct(
        mid_price=100.0, atr=1.0,
        sl_atr_mult=5.0, sl_floor_pct=1.0, sl_ceiling_pct=3.0)
    assert pct == pytest.approx(4.5)


def test_raw_width_below_floor_is_lifted_to_floor_before_slip():
    # raw = (0.1 / 100) * 5 * 100 = 0.5 -> lifted to floor 1.0, then +1.5.
    pct = _h4_worst_case_stop_pct(
        mid_price=100.0, atr=0.1,
        sl_atr_mult=5.0, sl_floor_pct=1.0, sl_ceiling_pct=3.0)
    assert pct == pytest.approx(2.5)


def test_raw_width_above_ceiling_caps_at_one_and_a_half_ceiling():
    # raw = (10 / 100) * 5 * 100 = 50 -> clamped to ceiling 3.0, cap 4.5.
    pct = _h4_worst_case_stop_pct(
        mid_price=100.0, atr=10.0,
        sl_atr_mult=5.0, sl_floor_pct=1.0, sl_ceiling_pct=3.0)
    assert pct == pytest.approx(4.5)


def test_slip_widen_never_exceeds_one_and_a_half_ceiling():
    # Even when floor sits at the ceiling, the cap of 1.5x ceiling holds.
    pct = _h4_worst_case_stop_pct(
        mid_price=100.0, atr=50.0,
        sl_atr_mult=10.0, sl_floor_pct=8.0, sl_ceiling_pct=8.0)
    assert pct == pytest.approx(12.0)


def test_scales_with_atr_over_spot():
    # raw = (2 / 50) * 1.5 * 100 = 6.0 -> ceiling 5.0 -> +2.5 = 7.5.
    pct = _h4_worst_case_stop_pct(
        mid_price=50.0, atr=2.0,
        sl_atr_mult=1.5, sl_floor_pct=1.0, sl_ceiling_pct=5.0)
    assert pct == pytest.approx(7.5)


def test_raw_width_inside_band_keeps_value_before_slip():
    # raw = (1 / 100) * 2 * 100 = 2.0 (inside [1,3]) -> +1.5 = 3.5.
    pct = _h4_worst_case_stop_pct(
        mid_price=100.0, atr=1.0,
        sl_atr_mult=2.0, sl_floor_pct=1.0, sl_ceiling_pct=3.0)
    assert pct == pytest.approx(3.5)


def test_requires_keyword_arguments():
    with pytest.raises(TypeError):
        _h4_worst_case_stop_pct(100.0, 1.0, 5.0, 1.0, 3.0)
