"""Unit tests for active unstucking (P1-1)."""

from __future__ import annotations

import copy
import time

import pytest

from hermes_trader.agents import unstick as us


NOW = 1_000_000.0  # fixed "now" in seconds


def _pos(coin, *, side="long", entry=100.0, mark=None, age_min=60,
         notional=500.0, upct=None):
    p = {
        "coin": coin,
        "side": side,
        "entry_px": entry,
        "entry_time": NOW - age_min * 60,
        "notional_usd": notional,
    }
    if mark is not None:
        p["mark_px"] = mark
    if upct is not None:
        p["unrealized_pct"] = upct
    return p


def test_unrealized_pct_long_and_short():
    assert us.unrealized_pct(_pos("A", entry=100, mark=95)) == pytest.approx(-0.05)
    # short profits when price falls
    s = _pos("B", side="short", entry=100, mark=95)
    assert us.unrealized_pct(s) == pytest.approx(0.05)
    # cached fallback when no mark available
    assert us.unrealized_pct(_pos("C", upct=-0.03)) == pytest.approx(-0.03)


def test_only_losing_positions_ranked():
    win = _pos("W", entry=100, mark=110)
    loss = _pos("L", entry=100, mark=94)
    ranked = us.rank_stuck([win, loss], NOW)
    coins = [p["coin"] for p, _ in ranked]
    assert coins == ["L"]


def test_mild_loss_close_to_market_is_actionable():
    p = _pos("A", entry=100, mark=94)  # -6%
    s = us.urgency_score(p, NOW)
    assert s > 0
    # severity capped: a -30% position doesn't score higher on loss alone.
    deep = _pos("D", entry=100, mark=70, age_min=60)
    mild = _pos("M", entry=100, mark=94, age_min=60)
    assert us.urgency_score(mild, NOW) >= us.urgency_score(deep, NOW)


def test_age_increases_urgency():
    young = _pos("A", entry=100, mark=94, age_min=10)
    old = _pos("B", entry=100, mark=94, age_min=400)
    assert us.urgency_score(old, NOW) > us.urgency_score(young, NOW)


def test_deterministic_ordering():
    a = _pos("ZZZ", entry=100, mark=94)
    b = _pos("AAA", entry=100, mark=94)
    ranked = us.rank_stuck([a, b], NOW)
    assert [p["coin"] for p, _ in ranked] == ["AAA", "ZZZ"]


def test_drawdown_pressure():
    assert us.drawdown_pressure(900, 1000, 10) is True
    assert us.drawdown_pressure(950, 1000, 10) is False
    assert us.drawdown_pressure(-1, 1000, 10) is False


def test_no_action_without_trigger():
    positions = [_pos("A", entry=100, mark=94)]
    selected, ranked, reason = us.select_unstucks(
        positions, NOW, equity_usd=990, peak_equity_usd=1000,
        max_peak_drawdown_pct=10)
    assert selected == [] and reason is None and len(ranked) == 1


def test_action_on_drawdown_selects_one():
    positions = [_pos("A", entry=100, mark=93, age_min=200),
                 _pos("B", entry=100, mark=94, age_min=200)]
    selected, remaining, reason = us.select_unstucks(
        positions, NOW, equity_usd=850, peak_equity_usd=1000,
        max_peak_drawdown_pct=10)
    assert reason == "drawdown_pressure"
    assert len(selected) == 1  # relieve one slot at a time
    assert len(remaining) == 1


def test_action_on_full_slots_frees_enough():
    positions = [_pos(c, entry=100, mark=94) for c in ("A", "B", "C")]
    selected, remaining, reason = us.select_unstucks(
        positions, NOW, equity_usd=1000, peak_equity_usd=1000,
        max_peak_drawdown_pct=10, max_active_slots=2, currently_stuck=3)
    assert reason == "stuck_slots_full"
    # need = 3 - 2 + 1 = 2 freed
    assert len(selected) == 2 and len(remaining) == 1


def test_pure_no_mutation():
    p = _pos("A", entry=100, mark=94)
    before = copy.deepcopy(p)
    us.urgency_score(p, NOW)
    us.select_unstucks([p], NOW, equity_usd=850, peak_equity_usd=1000,
                       max_peak_drawdown_pct=10)
    assert p == before


def test_bad_fields_do_not_raise():
    p = {"coin": "A", "entry_px": "x", "entry_time": None,
         "mark_px": "bad", "side": 1}
    assert us.urgency_score(p, NOW) == 0.0
    sel, _, _ = us.select_unstucks(
        [p], NOW, equity_usd="x", peak_equity_usd=None,
        max_peak_drawdown_pct=10)
    assert sel == []


def test_entry_time_millis_handled():
    # Real epoch magnitudes: seconds ~1.7e9, millis ~1.7e12. Build a ms
    # entry_time 60 minutes before a realistic "now".
    real_now = time.time()
    p = {"coin": "A", "side": "long", "entry_px": 100,
          "entry_time": (real_now - 3600) * 1000, "mark_px": 94,
          "notional_usd": 500}
    assert us._age_minutes(p, real_now) == pytest.approx(60.0, abs=0.1)


def test_from_hl_position_adapter():
    raw = {"type": "oneWay", "position": {
        "coin": "ETH", "szi": "0.5", "entryPx": "100",
        "markPx": "96", "positionValue": "48"}}
    j = us.from_hl_position(raw)
    assert j["coin"] == "ETH" and j["side"] == "long"
    assert j["entry_px"] == 100 and j["mark_px"] == 96
    assert j["notional_usd"] == 48
    # negative szi -> short
    raw["position"]["szi"] = "-0.5"
    assert us.from_hl_position(raw)["side"] == "short"
    # zero szi -> None (closed)
    raw["position"]["szi"] = "0"
    assert us.from_hl_position(raw) is None
    # malformed -> None
    assert us.from_hl_position({"x": 1}) is None


def test_entry_time_seconds_handled():
    real_now = time.time()
    p = {"coin": "A", "side": "long", "entry_px": 100,
          "entry_time": real_now - 3600, "mark_px": 94,
          "notional_usd": 500}
    assert us._age_minutes(p, real_now) == pytest.approx(60.0, abs=0.1)
