"""Tests for honest paper fills (R2): taker slippage on open/close."""
from __future__ import annotations

import time

import pytest

from hermes_trader.agents import shadow_book as sb

_T0 = int(time.time() // 60 * 60) * 1000


def _book(tmp_path, monkeypatch, **cfg):
    """A book on default live modeling: 4.5bp/fill taker, 1.5bp/fill maker,
    round_trip_fills=2 and the default 5bp taker slippage."""
    base = {"enabled": True, "maker_shadow_enabled": True,
            "starting_balance": 10000.0, "taker_fee_pct": 0.045,
            "maker_fee_pct": 0.015, "round_trip_fills": 2}
    base.update(cfg)
    monkeypatch.setattr(sb, "_shadow_cfg", lambda: dict(base))
    return sb.ShadowBook(path=str(tmp_path / "s.json"))


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


# --------------------------------------------------- P1 cost observability (bps)
# cost_bps = fee_bps + slippage_bps holds per fill; the whole round-trip fee is
# booked on the CLOSE fill, so the entry leg carries fee_bps=0 and summing the
# two legs' cost_bps gives the round-trip cost (taker 5+14=19bp, maker 0+3=3bp).

def test_taker_open_fill_cost_bps_is_entry_slippage_only(tmp_path, monkeypatch):
    book = _book(tmp_path, monkeypatch)
    fill = book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                            size_usd=1000.0, leverage=1, analysis_id="c1")
    assert fill["fee_bps"] == 0.0
    assert fill["slippage_bps"] == 5.0
    assert fill["cost_bps"] == 5.0


def test_taker_close_fill_carries_round_trip_fee(tmp_path, monkeypatch):
    book = _book(tmp_path, monkeypatch)
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1, analysis_id="c2")
    pos = book.state["accounts"]["taker"]["positions"][0]
    close = book._close_position("taker", pos, exit_px=110.0, reason="test")

    assert close["fee_bps"] == 9.0        # 4.5bp x 2 legs
    assert close["slippage_bps"] == 5.0   # exit leg only
    assert close["cost_bps"] == 14.0
    # fee_bps is derived from the BOOKED fee_usd, so they can never drift.
    assert close["fee_bps"] == pytest.approx(
        close["fee_usd"] / close["notional_usd"] * 1e4)
    assert close["cost_bps"] == pytest.approx(
        close["fee_bps"] + close["slippage_bps"])


def test_maker_close_fill_cost_bps_is_fee_only(tmp_path, monkeypatch):
    book = _book(tmp_path, monkeypatch)
    book.shadow_open(coin="ETH", side="long", entry_px=200.0,
                     size_usd=1000.0, leverage=1, analysis_id="c3")
    order = book.state["accounts"]["maker_shadow"]["pending_orders"][0]
    open_fill = book._fill_maker_order(order, fill_px=199.9, filled_at=_T0)
    assert open_fill["fee_bps"] == 0.0
    assert open_fill["slippage_bps"] == 0.0
    assert open_fill["cost_bps"] == 0.0

    pos = book.state["accounts"]["maker_shadow"]["positions"][0]
    close = book._close_position("maker_shadow", pos, exit_px=205.0,
                                 reason="test")
    assert close["fee_bps"] == 3.0        # 1.5bp x 2 legs
    assert close["slippage_bps"] == 0.0   # passive fill, no taker slip
    assert close["cost_bps"] == 3.0
