"""ADR-0012 direction-aware scoring + lower_highs mirror + freshness verdict."""
from __future__ import annotations

import math

import pytest

from hermes_trader.indicators.triggers import (
    composite_score,
    higher_lows_1h,
    lower_highs_1h,
)
from hermes_trader.indicators.side_scoring import (
    FRESHNESS_ALLOW,
    FRESHNESS_BLOCK,
    FRESHNESS_SHADOW,
    LONG_ONLY_TRIGGERS,
    SHORT_ONLY_TRIGGERS,
    freshness_verdict,
    side_aligned_score,
    trigger_alignment,
)


def _hit(name, score=8.0, fired=True):
    return {"name": name, "score": score, "fired": fired}


# ── legacy equivalence (ADR-0012 §3.1) ────────────────────────────────────

def test_legacy_mode_matches_composite_score_exactly():
    hits = [_hit("pctMoveSpike"), _hit("uptrendMomentum", 6.0),
            _hit("downtrendMomentum", 4.0), _hit("rangeCompression", 0.0)]
    weights = {"pctMoveSpike": 0.5, "uptrendMomentum": 0.2,
               "downtrendMomentum": 0.0, "rangeCompression": 0.3}
    expected = composite_score(hits, weights)
    assert side_aligned_score("long", hits, weights, mode="legacy") == expected
    assert side_aligned_score("short", hits, weights, mode="legacy") == expected


def test_legacy_mode_is_side_independent():
    hits = [_hit("uptrendMomentum"), _hit("bearishReversalCandle")]
    weights = {"uptrendMomentum": 0.2, "bearishReversalCandle": 0.0}
    assert (side_aligned_score("long", hits, weights, "legacy")
            == side_aligned_score("short", hits, weights, "legacy"))


def test_nan_fails_closed_like_composite():
    hits = [_hit("pctMoveSpike", float("nan"))]
    weights = {"pctMoveSpike": 1.0}
    assert side_aligned_score("long", hits, weights, "legacy") == 0.0
    assert side_aligned_score("long", hits, weights, "side_aware") == 0.0


def test_no_fired_hits_returns_zero():
    assert side_aligned_score("long", [_hit("x", fired=False)], {"x": 1.0},
                              "side_aware") == 0


# ── B1 directional scoring (ADR-0012 §3.2) ────────────────────────────────

def test_short_candidate_not_inflated_by_bullish_trigger():
    # A short candidate that only fired a bullish trigger: legacy gives it
    # credit, side_aware must score it 0.
    hits = [_hit("uptrendMomentum", 10.0)]
    weights = {"uptrendMomentum": 0.2}
    legacy = side_aligned_score("short", hits, weights, "legacy")
    aware = side_aligned_score("short", hits, weights, "side_aware")
    assert legacy > 0
    assert aware == 0.0


def test_long_candidate_not_inflated_by_bearish_trigger():
    hits = [_hit("downtrendMomentum", 10.0)]
    weights = {"downtrendMomentum": 0.2}
    assert side_aligned_score("long", hits, weights, "side_aware") == 0.0
    assert side_aligned_score("short", hits, weights, "side_aware") > 0


def test_neutral_trigger_counts_for_both_sides():
    hits = [_hit("pctMoveSpike", 10.0)]
    weights = {"pctMoveSpike": 1.0}
    assert (side_aligned_score("long", hits, weights, "side_aware")
            == side_aligned_score("short", hits, weights, "side_aware")
            > 0)


def test_side_aware_denominator_is_total_weight_not_aligned_only():
    # bullish + bearish both fire with equal weight; aligned contribution must
    # be divided by the TOTAL weight, so one side can't normalise to 100.
    hits = [_hit("uptrendMomentum", 10.0), _hit("downtrendMomentum", 10.0)]
    weights = {"uptrendMomentum": 0.2, "downtrendMomentum": 0.2}
    long_score = side_aligned_score("long", hits, weights, "side_aware")
    # raw = (10*0.2)/(0.4)*10 = 50, not 100
    assert long_score == pytest.approx(50.0)


def test_trigger_alignment_table_partition():
    assert not (LONG_ONLY_TRIGGERS & SHORT_ONLY_TRIGGERS)
    assert trigger_alignment("uptrendMomentum", "long") == 1
    assert trigger_alignment("uptrendMomentum", "short") == -1
    assert trigger_alignment("downtrendMomentum", "short") == 1
    assert trigger_alignment("breakout", "long") == 1
    assert trigger_alignment("breakout", "short") == 1


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        side_aligned_score("long", [], {}, mode="bogus")


# ── lower_highs mirror (ADR-0012 §3.3) ────────────────────────────────────

def _candles(highs, lows):
    return [{"h": h, "l": lo, "c": h, "o": h} for h, lo in zip(highs, lows)]


def test_lower_highs_mirrors_higher_lows_on_inverted_series():
    # Strictly descending highs -> lower_highs fires; ascending lows mirror.
    descending = _candles([10, 9, 8, 7, 6, 5, 4], [9, 8, 7, 6, 5, 4, 3])
    ascending = _candles([11, 10, 9, 8, 7, 6, 5], [1, 2, 3, 4, 5, 6, 7])
    lh = lower_highs_1h(descending, required=4)
    hl = higher_lows_1h(ascending, required=4)
    assert lh["fired"] is True and lh["name"] == "lowerHighs1h"
    assert hl["fired"] is True and hl["name"] == "higherLows1h"
    assert lh["score"] == pytest.approx(hl["score"])


def test_lower_highs_flat_when_ascending():
    ascending = _candles([4, 5, 6, 7, 8, 9, 10], [3, 4, 5, 6, 7, 8, 9])
    assert lower_highs_1h(ascending, required=4)["fired"] is False


def test_lower_highs_insufficient_history():
    short = _candles([1, 2], [0, 1])
    assert lower_highs_1h(short)["fired"] is False
    assert lower_highs_1h(short)["reason"] == "insufficient_history"


# ── freshness verdict (ADR-0012 §3.4) ────────────────────────────────────

def test_freshness_four_quadrants_enforce():
    assert freshness_verdict("long", {"fresh_impulse": True}, "enforce")["decision"] == FRESHNESS_BLOCK
    assert freshness_verdict("long", {"fresh_impulse": False}, "enforce")["decision"] == FRESHNESS_ALLOW
    assert freshness_verdict("short", {"fresh_impulse": True}, "enforce")["decision"] == FRESHNESS_ALLOW
    assert freshness_verdict("short", {"fresh_impulse": False}, "enforce")["decision"] == FRESHNESS_ALLOW


def test_freshness_shadow_mode_never_blocks():
    for side, fresh in [("long", True), ("long", False), ("short", True), ("short", False)]:
        out = freshness_verdict(side, {"fresh_impulse": fresh}, "shadow")
        assert out["decision"] == FRESHNESS_SHADOW
        # the counter-factual is still recorded for reconciliation
        assert out["would_block"] == (fresh and side == "long")


def test_freshness_shadow_is_default():
    out = freshness_verdict("long", {"fresh_impulse": True})
    assert out["mode"] == "shadow" and out["decision"] == FRESHNESS_SHADOW


def test_freshness_validates_inputs():
    with pytest.raises(ValueError):
        freshness_verdict("west", {"fresh_impulse": True})
    with pytest.raises(ValueError):
        freshness_verdict("long", {}, mode="bogus")
