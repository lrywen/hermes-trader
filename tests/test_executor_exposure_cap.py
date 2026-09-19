"""P1-1 step ③ — characterization for _sizing_exposure_cap (S9 leaf).

Pins the aggregate-exposure room narrowing of the per-trade notional cap
extracted from maybe_execute: max_total_notional_pct disabled leaves the base
cap, positive room takes the tighter of base/room, room with a disabled base
cap (0) becomes the room itself, and non-positive room leaves the base cap.
"""
from __future__ import annotations

from hermes_trader.agents import executor

cap = executor._sizing_exposure_cap


def test_pct_disabled_keeps_base():
    # agg equity 100 * pct 0 → no room constraint.
    assert cap({"max_total_notional_pct": 0}, 100.0, 0.0, 30.0) == 30.0
    assert cap({}, 100.0, 50.0, 30.0) == 30.0


def test_positive_room_takes_min_of_base_and_room():
    # pct 3.0 * equity 100 = 300 cap; open 250 → room 50 → min(80, 50) = 50.
    assert cap({"max_total_notional_pct": 3.0}, 100.0, 250.0, 80.0) == 50.0
    # Room larger than base → base binds.
    assert cap({"max_total_notional_pct": 3.0}, 100.0, 100.0, 40.0) == 40.0


def test_zero_base_cap_uses_room():
    # Per-trade cap disabled (0) but room is 60 → cap becomes the room.
    assert cap({"max_total_notional_pct": 3.0}, 100.0, 240.0, 0.0) == 60.0


def test_nonpositive_room_leaves_base():
    # cap 300, open 320 → room -20 → base untouched.
    assert cap({"max_total_notional_pct": 3.0}, 100.0, 320.0, 30.0) == 30.0
    # Exactly zero room (book at the cap) → base untouched.
    assert cap({"max_total_notional_pct": 3.0}, 100.0, 300.0, 30.0) == 30.0
