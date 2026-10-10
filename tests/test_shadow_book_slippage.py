"""Tests for honest paper fills (R2): taker slippage on open/close."""
from __future__ import annotations

from hermes_trader.agents import shadow_book as sb


def test_long_open_pays_more():
    out = sb._apply_slippage(100.0, side="long", opening=True, slip_pct=0.05)
    assert out == 100.05


def test_short_open_receives_less():
    out = sb._apply_slippage(100.0, side="short", opening=True, slip_pct=0.05)
    assert out == 99.95


def test_long_close_receives_less():
    # Closing a long = selling -> price moves down.
    out = sb._apply_slippage(100.0, side="long", opening=False, slip_pct=0.05)
    assert out == 99.95


def test_short_close_pays_more():
    # Closing a short = buying -> price moves up.
    out = sb._apply_slippage(100.0, side="short", opening=False, slip_pct=0.05)
    assert out == 100.05


def test_zero_slippage_is_identity():
    assert sb._apply_slippage(100.0, side="long", opening=True,
                              slip_pct=0.0) == 100.0


def test_slippage_default_nonnegative(monkeypatch):
    # With no override configured, helper returns the conservative default.
    monkeypatch.setattr(sb, "_shadow_cfg", lambda: {})
    assert sb._taker_slippage_pct() == 0.05


def test_slippage_override(monkeypatch):
    monkeypatch.setattr(sb, "_shadow_cfg",
                        lambda: {"taker_slippage_pct": 0.1})
    assert sb._taker_slippage_pct() == 0.1


def test_slippage_disabled_via_zero(monkeypatch):
    monkeypatch.setattr(sb, "_shadow_cfg",
                        lambda: {"taker_slippage_pct": 0})
    assert sb._taker_slippage_pct() == 0.0
