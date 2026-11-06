"""Tests for hermes_trader/agents/bracket_geometry.py (P0 port from pathiel).

Anchors on the venue-verified number recorded upstream: a 20x Brent long from
107.16 liquidates at ~104.41 (the venue showed 104.43; the gap is fees+funding).
"""
from __future__ import annotations

import pytest

from hermes_trader.agents.bracket_geometry import (
    DEFAULT_MAX_STOP_FRAC_OF_LIQ,
    DEFAULT_MIN_R,
    TARGET_OFFSET_FRAC,
    check_levels,
    check_leverage,
    liq_estimate,
    max_leverage_for_stop,
    offset_target,
)

# ── liq_estimate ─────────────────────────────────────────────────────────────

def test_liq_estimate_matches_venue_anchor_long():
    # 20x long from 107.16 on a coin whose max leverage is 20 (mmr=1/40=0.025).
    liq = liq_estimate(107.16, leverage=20, max_leverage=20, is_long=True)
    assert liq == pytest.approx(104.41, abs=0.05)


def test_liq_estimate_short_is_mirror():
    liq = liq_estimate(100.0, leverage=20, max_leverage=20, is_long=False)
    assert liq > 100.0  # a short is liquidated on an up-move


def test_liq_estimate_higher_leverage_liquidates_closer():
    near = liq_estimate(100.0, leverage=20, max_leverage=50, is_long=True)
    far = liq_estimate(100.0, leverage=5, max_leverage=50, is_long=True)
    assert abs(100.0 - near) < abs(100.0 - far)


# ── max_leverage_for_stop ────────────────────────────────────────────────────

def test_max_leverage_for_stop_tight_stop_allows_high_leverage():
    # 0.5% stop on a 50x-max coin: a tight stop leaves room for high leverage.
    lev = max_leverage_for_stop(100.0, 99.5, max_leverage=50)
    assert lev > 1


def test_max_leverage_for_stop_wide_stop_forces_low_leverage():
    # 8% stop: at high leverage liquidation is closer than the stop → cap low.
    lev = max_leverage_for_stop(100.0, 92.0, max_leverage=50)
    assert lev <= 6  # 1/0.08 ≈ 12.5x before the 0.5 fraction halves it


def test_max_leverage_for_stop_zero_distance_returns_one():
    assert max_leverage_for_stop(100.0, 100.0, max_leverage=50) == 1


def test_max_leverage_never_exceeds_coin_max():
    lev = max_leverage_for_stop(100.0, 99.95, max_leverage=20)
    assert lev <= 20


# ── check_leverage ───────────────────────────────────────────────────────────

def test_check_leverage_rejects_stop_beyond_liquidation():
    # 20x with an 8% stop: liquidation ~2.5% away, stop far outside → refuse.
    out = check_leverage(100.0, 92.0, leverage=20, coin_max_leverage=20)
    assert out["ok"] is False
    assert "max_leverage_ok" in out
    assert out["max_leverage_ok"] < 20
    assert "stop uses" in out["reason"]


def test_check_leverage_accepts_tight_stop_at_moderate_leverage():
    out = check_leverage(100.0, 99.0, leverage=5, coin_max_leverage=20)
    assert out["ok"] is True
    assert out["stop_frac_of_liq"] <= DEFAULT_MAX_STOP_FRAC_OF_LIQ + 1e-9
    assert out["liq_px"] < 100.0


def test_check_leverage_rejects_nonpositive_inputs():
    out = check_leverage(0.0, 99.0, leverage=5, coin_max_leverage=20)
    assert out["ok"] is False


def test_check_leverage_reports_distances():
    out = check_leverage(100.0, 99.0, leverage=5, coin_max_leverage=20)
    assert out["stop_dist_pct"] == pytest.approx(1.0, abs=0.01)
    assert out["liq_dist_pct"] > 0


# ── check_levels ─────────────────────────────────────────────────────────────

def test_check_levels_long_valid():
    out = check_levels(100.0, 95.0, 115.0)  # risk 5, reward 15 → 3R
    assert out["ok"] is True
    assert out["side"] == "long"
    assert out["r_multiple"] == pytest.approx(3.0)


def test_check_levels_short_valid():
    out = check_levels(100.0, 105.0, 85.0)  # risk 5, reward 15 → 3R
    assert out["ok"] is True
    assert out["side"] == "short"


def test_check_levels_rejects_entry_not_between():
    out = check_levels(100.0, 105.0, 110.0)  # stop above entry but tp also above
    assert out["ok"] is False
    assert "between" in out["reason"]


def test_check_levels_rejects_under_min_r():
    out = check_levels(100.0, 95.0, 102.0)  # risk 5, reward 2 → 0.4R < 2R
    assert out["ok"] is False
    assert "under the" in out["reason"]
    assert out["r_multiple"] == pytest.approx(0.4)


def test_check_levels_rejects_nonpositive():
    assert check_levels(0.0, 95.0, 115.0)["ok"] is False


def test_check_levels_custom_min_r():
    out = check_levels(100.0, 95.0, 107.5, min_r=1.5)  # 1.5R exactly
    assert out["ok"] is True


# ── offset_target ────────────────────────────────────────────────────────────

def test_offset_target_long_moves_down():
    assert offset_target(115.0, "long") == pytest.approx(115.0 * (1 - TARGET_OFFSET_FRAC))


def test_offset_target_short_moves_up():
    assert offset_target(85.0, "short") == pytest.approx(85.0 * (1 + TARGET_OFFSET_FRAC))


def test_offset_target_zero_frac_is_identity():
    assert offset_target(115.0, "long", frac=0.0) == 115.0


# ── defaults sanity ──────────────────────────────────────────────────────────

def test_defaults():
    assert DEFAULT_MIN_R == 2.0
    assert DEFAULT_MAX_STOP_FRAC_OF_LIQ == 0.5
    assert TARGET_OFFSET_FRAC == 0.005
