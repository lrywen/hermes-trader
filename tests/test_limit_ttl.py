"""Unit tests for the limit-with-TTL execution plan (P2)."""

from __future__ import annotations

import pytest

from hermes_trader.execution import limit_ttl as lt


def test_limit_price_passive_buy_below_sell_above():
    assert lt.limit_price(100, True, 10) == pytest.approx(99.9)
    assert lt.limit_price(100, False, 10) == pytest.approx(100.1)


def test_limit_price_cross_inverts_offsets():
    assert lt.limit_price(100, True, 10, cross=True) == pytest.approx(100.1)
    assert lt.limit_price(100, False, 10, cross=True) == pytest.approx(99.9)


def test_expected_improvement_and_maker_gate():
    # spread 20bps half=10, offset 4 -> improvement 6bps
    assert lt.expected_maker_improvement_bps(20, 4) == pytest.approx(6)
    # offset larger than half spread -> no improvement -> 0
    assert lt.expected_maker_improvement_bps(10, 10) == 0
    assert lt.should_attempt_maker(spread_bps=20, offset_bps=4,
                                   min_improvement_bps=5) is True
    assert lt.should_attempt_maker(spread_bps=12, offset_bps=4,
                                   min_improvement_bps=5) is False


def test_ttl_expired():
    assert lt.ttl_expired(0, 5, 10) is False
    assert lt.ttl_expired(0, 10, 10) is True
    assert lt.ttl_expired(0, 100, -1) is False  # invalid -> never


def test_advance_filled_is_terminal():
    s = {"status": "filled", "submitted_at": 0, "ttl_s": 10}
    state, plan = lt.advance(s, 999, is_filled=False, still_open=True)
    assert state == "filled" and plan["action"] == "none"


def test_advance_within_ttl_waits():
    s = {"status": "resting", "submitted_at": 0, "ttl_s": 10}
    state, plan = lt.advance(s, 5, is_filled=False, still_open=True)
    assert state == "resting" and plan["action"] == "wait"


def test_advance_fill_during_resting():
    s = {"status": "resting", "submitted_at": 0, "ttl_s": 10}
    state, plan = lt.advance(s, 3, is_filled=True, still_open=False)
    assert state == "filled" and plan["action"] == "none"


def test_advance_expired_still_open_cancels_then_taker():
    s = {"status": "resting", "submitted_at": 0, "ttl_s": 10}
    state, plan = lt.advance(s, 11, is_filled=False, still_open=True)
    assert state == "expired_open" and plan["action"] == "cancel"
    # next tick after cancel -> taker fallback regardless of open state
    s2 = {"status": "expired_open", "submitted_at": 0, "ttl_s": 10}
    state, plan = lt.advance(s2, 12, is_filled=False, still_open=True)
    assert state == "fallback_taker" and plan["action"] == "place_taker"


def test_advance_expired_gone_goes_straight_to_taker():
    s = {"status": "resting", "submitted_at": 0, "ttl_s": 10}
    state, plan = lt.advance(s, 11, is_filled=False, still_open=False)
    assert state == "fallback_taker" and plan["action"] == "place_taker"


def test_advance_bad_inputs_do_not_raise():
    s = {"status": "resting", "submitted_at": "x", "ttl_s": -1}
    # invalid ttl -> never expires -> keeps waiting
    state, plan = lt.advance(s, 999, is_filled=False, still_open=True)
    assert state == "resting" and plan["action"] == "wait"
