"""Unit tests for the launch-ignition confirmation gate.

Covers the pure detector (volume >= mult x prior-avg AND close breaks the
prior range; long/short; only closed, post-entry bars) and its wiring into
DSLTracker.check() as the ``ignite_timeout`` exit, plus state round-trip.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hermes_trader.agents.dsl_exit import (
    DSLTracker,
    ExitPolicy,
    detect_ignition,
    _tracker_from_dict,
    _tracker_to_dict,
)

BAR_MS = 15 * 60_000


def _bar(i, o, h, l, c, v, base_ms=None):
    if base_ms is None:
        # default: well in the past so bars are closed
        base_ms = time.time() * 1000 - 100 * BAR_MS
    return SimpleNamespace(t=int(base_ms + i * BAR_MS), o=o, h=h, l=l, c=c, v=v)


def _flat_bars(n, v=100.0, px=100.0, start=0):
    return [_bar(start + i, px, px + 1, px - 1, px, v) for i in range(n)]


def _policy(**kw) -> ExitPolicy:
    base = dict(max_loss_pct=3.5, max_loss_roe_pct=100.0, protect_pct=1.0,
                retrace_threshold=0.40, hard_timeout_minutes=99999.0,
                stale_flat_timeout_minutes=0.0, hard_stop_confirm_sec=0.0,
                ignite_grace_minutes=90.0, ignite_vol_mult=2.0,
                ignite_periods=("5m", "15m", "1h"))
    base.update(kw)
    return ExitPolicy(**base)


# ---------- detect_ignition ----------

def test_long_ignites_on_volume_and_breakout():
    bars = _flat_bars(20)                      # indices 0..19, all closed
    entry_s = (bars[0].t / 1000) - 1
    # bar 20: 2.5x volume, closes above prior high (101)
    bars.append(_bar(20, 100, 103, 100, 102.5, 250))
    ig = detect_ignition(bars, entry_s, True)
    assert ig == bars[20].t


def test_high_volume_without_breakout_does_not_ignite():
    bars = _flat_bars(20)
    entry_s = bars[0].t / 1000 - 1
    bars.append(_bar(20, 100, 100.5, 99.5, 100.2, 250))  # inside prior range
    assert detect_ignition(bars, entry_s, True) is None


def test_breakout_without_volume_does_not_ignite():
    bars = _flat_bars(20)
    entry_s = bars[0].t / 1000 - 1
    bars.append(_bar(20, 100, 103, 100, 102.5, 100))     # no volume expansion
    assert detect_ignition(bars, entry_s, True) is None


def test_short_ignites_on_downside_break():
    bars = _flat_bars(20)
    entry_s = bars[0].t / 1000 - 1
    bars.append(_bar(20, 100, 100.5, 97, 97.5, 250))     # below prior low 99
    assert detect_ignition(bars, entry_s, False) == bars[20].t


def test_bars_before_entry_are_ignored():
    bars = _flat_bars(25)
    # entry is placed at bar 22; launch-looking bar at index 10 must not count
    entry_s = bars[22].t / 1000 + 1
    assert detect_ignition(bars, entry_s, True) is None


def test_unclosed_bar_is_ignored():
    bars = _flat_bars(20)
    entry_s = bars[0].t / 1000 - 1
    # forming bar: t in the future so t + period > now
    future = time.time() * 1000
    bars.append(_bar(0, 100, 103, 100, 102.5, 250, base_ms=future))
    assert detect_ignition(bars, entry_s, True) is None


# ---------- DSLTracker.check wiring ----------

def _tracker(policy, age_min):
    t = DSLTracker("DOGE", "long", 100.0, time.time() - age_min * 60,
                   policy, leverage=3)
    return t


def test_check_exits_unignited_after_grace():
    t = _tracker(_policy(), 91)
    v = t.check(100.0)
    assert v.exit is True
    assert "ignite_timeout" in v.reason


def test_check_holds_before_grace():
    t = _tracker(_policy(), 80)
    v = t.check(100.0)
    assert v.exit is False


def test_check_holds_after_grace_when_ignited():
    t = _tracker(_policy(), 95)
    t.mark_ignited()
    v = t.check(100.0)
    assert v.exit is False


def test_mark_ignited_is_monotonic():
    t = _tracker(_policy(), 10)
    assert t.mark_ignited() is True
    assert t.mark_ignited() is False


def test_mark_ignited_records_period():
    t = _tracker(_policy(), 10)
    t.mark_ignited(period="5m")
    assert t.ignite_period == "5m"


def test_state_roundtrip_preserves_ignition():
    t = _tracker(_policy(), 30)
    t.mark_ignited(period="1h")
    d = _tracker_to_dict(t)
    t2 = _tracker_from_dict(d)
    assert t2.ignited is True
    assert t2.ignite_ts is not None
    assert t2.ignite_period == "1h"
    assert t2.policy.ignite_grace_minutes == 90.0
    assert t2.policy.ignite_periods == ("5m", "15m", "1h")


def test_old_state_without_ignite_fields_hydrates_unignited():
    t = _tracker(_policy(), 30)
    d = _tracker_to_dict(t)
    del d["ignited"]
    del d["ignite_ts"]
    t2 = _tracker_from_dict(d)
    assert t2.ignited is False
    assert t2.policy.ignite_grace_minutes == 90.0


# ---------- _parse_ignite_periods ----------

def test_parse_periods_drops_unknown_and_keeps_order():
    from hermes_trader.agents.dsl_exit import _parse_ignite_periods
    assert _parse_ignite_periods(["1h", "junk", "5m"]) == ("1h", "5m")


def test_parse_periods_accepts_string():
    from hermes_trader.agents.dsl_exit import _parse_ignite_periods
    assert _parse_ignite_periods("15m") == ("15m",)


def test_parse_periods_falls_back_when_all_invalid():
    from hermes_trader.agents.dsl_exit import _parse_ignite_periods
    assert _parse_ignite_periods(["x", 3]) == ExitPolicy.ignite_periods
