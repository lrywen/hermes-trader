"""Unit tests for continuous coin-selection scoring (P1-2)."""

from __future__ import annotations

import copy
import math

import pytest

from hermes_trader.agents import coin_select as cs


def _m(coin, *, ntl=1e8, prev=100, mark=105, fund=0.0001, oi=1e6):
    return {"coin": coin, "dayNtlVlm": ntl, "prevDayPx": prev,
            "markPx": mark, "funding": fund, "openInterest": oi}


def test_turnover_log_scaled_bounds():
    assert cs.turnover_term(_m("A", ntl=1e5)) == pytest.approx(0.0, abs=1e-6)
    assert cs.turnover_term(_m("A", ntl=1e9)) == pytest.approx(1.0, abs=1e-6)
    assert cs.turnover_term({"dayNtlVlm": 0}) == 0


def test_momentum_term():
    assert cs.momentum_term(_m("A", prev=100, mark=110)) == pytest.approx(1.0)
    assert cs.momentum_term(_m("A", prev=100, mark=102)) == pytest.approx(0.2)
    # explicit cur price overrides mark
    assert cs.momentum_term(_m("A"), cur_px=110) == pytest.approx(1.0)


def test_extreme_funding_lowers_structure_score():
    mild = cs.funding_oi_term(_m("A", fund=0.00005))
    extreme = cs.funding_oi_term(_m("B", fund=0.001))
    assert mild > extreme
    assert cs.funding_oi_term({"openInterest": 0}) == 0


def test_pre_candle_score_bounded_and_liquid_momentum_wins():
    weak = _m("LOW", ntl=1e6, prev=100, mark=100)
    strong = _m("HIGH", ntl=1e9, prev=100, mark=110)
    assert 0 <= cs.pre_candle_score(weak) <= 100
    assert cs.pre_candle_score(strong) > cs.pre_candle_score(weak)


def test_rank_pool_deterministic_ties_by_coin():
    a = _m("ZZZ")
    b = _m("AAA")
    ranked = cs.rank_pool([a, b])
    assert [m["coin"] for m, _ in ranked] == ["AAA", "ZZZ"]


def test_top_k_edge_cases():
    pool = [_m(c) for c in ("A", "B", "C")]
    assert cs.top_k(pool, 2) and len(cs.top_k(pool, 2)) == 2
    assert cs.top_k(pool, 0) == []
    assert len(cs.top_k(pool, 10)) == 3


def test_custom_weights_normalised():
    m = _m("A")
    # Only momentum weighted.
    w = {"turnover": 0, "momentum": 1, "funding_oi": 0}
    # momentum: mark 105 vs prev 100 -> 0.5 -> score 50
    assert cs.pre_candle_score(m, weights=w) == pytest.approx(50.0)


def test_log_range_volatility_and_ema_readiness_bounds():
    # steadily rising series -> positive EMA readiness, some volatility
    closes = [100 + i for i in range(25)]
    v = cs.log_range_volatility(closes)
    r = cs.ema_readiness(closes)
    assert 0 <= v <= 1 and 0 <= r <= 1
    assert cs.log_range_volatility([100]) == 0
    assert cs.ema_readiness([100]) == 0


def test_post_candle_readiness_blend():
    closes = [100 * (1.002 ** i) for i in range(30)]
    q = cs.post_candle_readiness(closes)
    assert 0 <= q <= 1
    # Smooth uptrend: EMA readiness is high even though log-range vol is low.
    assert cs.ema_readiness(closes) > 0.8
    assert q < cs.ema_readiness(closes)  # low vol drags the blend down


def test_pure_no_mutation():
    m = _m("A")
    before = copy.deepcopy(m)
    cs.pre_candle_score(m)
    cs.rank_pool([m])
    assert m == before


def test_bad_fields_do_not_raise():
    m = {"coin": "A", "dayNtlVlm": "x", "prevDayPx": None,
          "markPx": "bad", "funding": "f", "openInterest": "o"}
    assert cs.pre_candle_score(m) == 0.0
    assert cs.log_range_volatility(["x", None, 100]) == 0
