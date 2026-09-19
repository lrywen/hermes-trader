"""P1-1 step ③ — characterization for _v1_stop_width (S9 leaf).

Pins the legacy v1 stop-width formula extracted from maybe_execute:
min(max_loss_pct, max_loss_roe_pct / max(1, leverage)) / 100, with the
historical defaults 0.4% / 5.0%. This is the top-level DSL stop used by v1
sizing and as the baseline for the v2 shadow comparison.
"""
from __future__ import annotations

from hermes_trader.agents import executor

stop_width = executor._v1_stop_width


def test_defaults_are_historical_constants():
    # leverage 10 → min(0.4, 5.0/10=0.5) = 0.4% → 0.004.
    assert stop_width({}, leverage=10) == 0.004


def test_roe_binds_when_tighter_than_loss():
    # max_loss_pct 2.5, max_loss_roe_pct 10 at 10x → min(2.5, 1.0) = 1.0%.
    assert stop_width({"max_loss_pct": 2.5, "max_loss_roe_pct": 10.0},
                      leverage=10) == 0.01


def test_loss_binds_when_tighter_than_roe():
    # max_loss_pct 0.4, max_loss_roe_pct 25 at 10x → min(0.4, 2.5) = 0.4%.
    assert stop_width({"max_loss_pct": 0.4, "max_loss_roe_pct": 25.0},
                      leverage=10) == 0.004


def test_leverage_is_floor_clamped_to_one():
    # leverage 0.5 → clamp to 1 → min(0.4, 5.0/1=5.0) = 0.4%.
    assert stop_width({}, leverage=0.5) == 0.004