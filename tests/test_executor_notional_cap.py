"""P1-1 step ③ — characterization for _resolve_notional_cap (S9 leaf).

Pins the equity-tiered per-trade notional cap resolution extracted from
maybe_execute: absolute cap disabled (0) stays disabled, micro accounts keep
the hard floor below the tier threshold, larger accounts scale with equity
(tier multiple), and the C11-tunable threshold/multiple override the
historical constants. A cfg_get resolution failure falls back to constants
(fail-open), never raising.
"""
from __future__ import annotations

from hermes_trader.agents import executor
from hermes_trader.agents.executor import (
    _NOTIONAL_CAP_TIER_EQUITY_USD,
    _NOTIONAL_CAP_TIER_MULTIPLE,
)

resolve = executor._resolve_notional_cap


def test_zero_cap_stays_disabled():
    assert resolve({"max_trade_notional_usd": 0}, agg_equity=1_000.0) == 0.0


def test_micro_account_keeps_hard_floor():
    # equity 30 < tier threshold 50 → base cap unchanged.
    cap = resolve({"max_trade_notional_usd": 30.0}, agg_equity=30.0)
    assert cap == 30.0


def test_large_account_scales_with_equity():
    # equity 100 >= 50 → max(30, 100 * 1.5) = 150.
    cap = resolve({"max_trade_notional_usd": 30.0}, agg_equity=100.0)
    assert cap == 150.0


def test_tier_threshold_and_multiple_configurable(monkeypatch):
    cfg = {
        "max_trade_notional_usd": 30.0,
        "notional_cap_tier_equity_usd": 20.0,
        "notional_cap_tier_multiple": 2.0,
    }
    # equity 25 >= threshold 20 → max(30, 25*2) = 50.
    assert resolve(cfg, agg_equity=25.0) == 50.0


def test_resolution_failure_falls_back_to_constants(monkeypatch):
    def _boom(*a, **k):
        raise ValueError("config store unavailable")

    monkeypatch.setattr(executor, "cfg_get", _boom)
    # Historical tier threshold 50 / multiple 1.5 apply: equity 100 → 150.
    cap = resolve({"max_trade_notional_usd": 30.0}, agg_equity=100.0)
    assert cap == max(30.0, 100.0 * _NOTIONAL_CAP_TIER_MULTIPLE)
    # Below the historical threshold the base floor holds.
    assert resolve({"max_trade_notional_usd": 30.0},
                   agg_equity=_NOTIONAL_CAP_TIER_EQUITY_USD - 1) == 30.0
