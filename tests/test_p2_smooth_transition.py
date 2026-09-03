"""P2 candidate — smooth phase1→phase2 floor transition.

These tests pin the *contract* of the smooth-transition candidate
(``smooth_transition_enabled`` / ``smooth_band_pct``), NOT a claim that it
improves PnL. Tick-level A/B replay (scripts/p2_smooth_replay.py) showed it is
net-negative, so it ships DEFAULT OFF (inert); these tests guarantee:

1. Inertness/parity — with the flag off (the default) the phase-2 floor is
   byte-identical to the plain trailing floor, so enabling the code path in
   production config can never change behaviour unless an operator opts in.
2. Ramp shape — when enabled, the floor interpolates linearly (in
   favorable-offset space) from the hard stop at the arm instant up to the
   full trailing floor at the top of the band, and is correct for shorts too.
"""

import time

import pytest


def _base_kwargs():
    return dict(protect_pct=1.25, retrace_threshold=0.20,
                max_loss_pct=0.4, max_loss_roe_pct=5.0,
                breach_confirm_sec=0.0, hard_stop_confirm_sec=0.0)


def _tracker(monkeypatch, policy, side="long", entry=100.0):
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(dsl_exit, "_request_save", lambda **_k: None)
    return dsl_exit.DSLTracker("P2T", side, entry, time.time(),
                               policy=policy, leverage=10)


def test_smooth_defaults_off(monkeypatch):
    """The smooth switch defaults to OFF (inert)."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    pol = ExitPolicy()
    assert pol.smooth_transition_enabled is False
    assert pol.smooth_band_pct == 1.0


def test_smooth_off_floor_equals_plain_trail(monkeypatch):
    """Parity: with smooth OFF the phase-2 helper returns exactly the plain
    trailing floor at every peak level in the band."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    pol = ExitPolicy(**_base_kwargs(), smooth_transition_enabled=False)
    tr = _tracker(monkeypatch, pol)
    eff = tr._effective_max_loss()
    retrace = 0.20
    for peak in (101.25, 101.5, 101.75, 102.0, 102.25, 103.0):
        tr.peak_px = peak
        assert tr._smooth_phase2_floor(retrace, eff) == \
            pytest.approx(tr._trailing_floor(retrace))


def test_smooth_on_ramp_shape_long(monkeypatch):
    """Long ramp: at arm (peak=protect) floor == hard stop; at the band top
    (peak=protect+band) floor == full trail; midpoint is the exact linear
    interpolation between them."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    # band=1.0 → arm peak=101.25, band-top peak=102.25.
    pol = ExitPolicy(**_base_kwargs(), smooth_transition_enabled=True,
                     smooth_band_pct=1.0)
    tr = _tracker(monkeypatch, pol, side="long", entry=100.0)
    eff = tr._effective_max_loss()
    retrace = 0.20
    hard = tr._hard_stop_floor(eff)

    tr.peak_px = 101.25  # arm instant (progress=0)
    assert tr._smooth_phase2_floor(retrace, eff) == pytest.approx(hard)

    tr.peak_px = 102.25  # band top (progress=1)
    trail_top = tr._trailing_floor(retrace)  # depends on peak=102.25
    assert tr._smooth_phase2_floor(retrace, eff) == pytest.approx(trail_top)

    tr.peak_px = 101.75  # midpoint (progress=0.5)
    # trail at the midpoint peak; hard is fixed below entry.
    trail_mid = tr._trailing_floor(retrace)
    assert tr._smooth_phase2_floor(retrace, eff) == \
        pytest.approx(hard + (trail_mid - hard) * 0.5)


def test_smooth_on_ramp_shape_short(monkeypatch):
    """Short ramp mirrors the long one in favorable-offset space: at arm the
    floor == the (higher) hard stop; at the band top it == the (lower) full
    trail; interpolation stays between them."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    pol = ExitPolicy(**_base_kwargs(), smooth_transition_enabled=True,
                     smooth_band_pct=1.0)
    tr = _tracker(monkeypatch, pol, side="short", entry=100.0)
    eff = tr._effective_max_loss()
    retrace = 0.20
    hard = tr._hard_stop_floor(eff)  # above entry for a short

    tr.peak_px = 100.0 - 1.25  # arm instant (favorable peak profit)
    assert tr._smooth_phase2_floor(retrace, eff) == pytest.approx(hard)

    tr.peak_px = 100.0 - 2.25  # band top
    trail_top = tr._trailing_floor(retrace)
    assert tr._smooth_phase2_floor(retrace, eff) == pytest.approx(trail_top)
    # For a short the full trail must sit BELOW the hard stop.
    assert trail_top < hard


def test_smooth_clamped_outside_band(monkeypatch):
    """progress is clamped: below arm the helper never goes under the hard
    stop, and above the band top it never exceeds the full trail."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    pol = ExitPolicy(**_base_kwargs(), smooth_transition_enabled=True,
                     smooth_band_pct=1.0)
    tr = _tracker(monkeypatch, pol)
    eff = tr._effective_max_loss()
    retrace = 0.20
    hard = tr._hard_stop_floor(eff)

    tr.peak_px = 100.5  # not yet armed (peak < protect)
    assert tr._smooth_phase2_floor(retrace, eff) == pytest.approx(hard)

    tr.peak_px = 110.0  # well above band top
    assert tr._smooth_phase2_floor(retrace, eff) == \
        pytest.approx(tr._trailing_floor(retrace))


def test_smooth_off_vs_on_identical_when_band_crossed_fast(monkeypatch):
    """End-to-end parity: a run that rips straight through the band before any
    pull-back must exit at the SAME price/time for smooth-ON and smooth-OFF
    (the ramp is fully traversed before the breach, so it cannot diverge)."""
    from hermes_trader.agents.dsl_exit import ExitPolicy

    def _run(smooth_on):
        pol = ExitPolicy(**_base_kwargs(), smooth_transition_enabled=smooth_on,
                         smooth_band_pct=1.0)
        tr = _tracker(monkeypatch, pol)
        assert tr.check(100.0).exit is False
        assert tr.check(103.0).exit is False   # peak +3%, well past band
        v = tr.check(101.9)                     # pull back through trail
        return v

    v_off = _run(False)
    v_on = _run(True)
    assert v_off.exit is True and v_on.exit is True
    assert v_off.floor_price == pytest.approx(v_on.floor_price)
