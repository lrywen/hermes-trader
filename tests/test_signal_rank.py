"""Unit tests for the cross-signal ranking layer (P0-1).

These tests pin the pure scoring/selection contract: bounded scores,
deterministic ordering, no input mutation, and correct top-k splitting.
"""

from __future__ import annotations

import copy

import pytest

from hermes_trader.agents import signal_rank as sr


def _trigger(name: str, *, fired: bool, score: float) -> dict:
    return {"name": name, "score": score, "fired": fired, "reason": "x"}


def _perception(
    coin: str,
    *,
    composite: float = 50.0,
    triggers: list[dict] | None = None,
    whale=None,
    cvd: dict | None = None,
    volume_profile: dict | None = None,
    ptype: str = "crypto",
) -> dict:
    return {
        "id": f"{coin}-1",
        "coin": coin,
        "type": ptype,
        "composite_score": composite,
        "triggers": triggers if triggers is not None else [
            _trigger("breakout", fired=True, score=6.0)],
        "whale_signal": whale,
        "cvd_divergence": cvd,
        "volume_profile": volume_profile,
        "mid": 100.0,
    }


def _job(coin: str, perception: dict, score: float):
    return (coin, perception, score, {"pass": True})


def test_rank_score_bounded():
    p = _perception("AAA", composite=10_000.0)
    s = sr.rank_score(p, 10_000.0)
    assert 0.0 <= s <= 100.0


def test_rank_score_reaches_near_ceiling_when_all_components_max():
    p = _perception(
        "AAA", composite=100.0,
        triggers=[_trigger("breakout", fired=True, score=10.0),
                  _trigger("momentumBurst", fired=True, score=10.0),
                  _trigger("volumeBuildup1h", fired=True, score=10.0)],
        whale={"magnitude": 1.0},
        cvd={"strength_pct": 100.0},
        volume_profile={"readiness": 1.0})
    s = sr.rank_score(p, 100.0)
    # The five positive weights sum to 0.95 (the remaining 0.05 is the
    # class-penalty lever, which is 0 for a main-universe coin), so the max
    # positive score is 95.
    assert s == pytest.approx(95.0, abs=1e-6)


def test_rank_score_deterministic_and_pure():
    p = _perception("AAA")
    before = copy.deepcopy(p)
    s1 = sr.rank_score(p, 50.0)
    s2 = sr.rank_score(p, 50.0)
    assert s1 == s2
    assert p == before  # no mutation


def test_composite_dominates_ranking():
    weak = _job("LOW", _perception("LOW", composite=30.0), 30.0)
    strong = _job("HIGH", _perception("HIGH", composite=80.0), 80.0)
    ranked = sr.rank_jobs([weak, strong])
    assert ranked[0][0][0] == "HIGH"
    assert ranked[0][1] > ranked[1][1]


def test_ties_broken_by_coin_name_deterministically():
    a = _job("ZZZ", _perception("ZZZ", composite=50.0), 50.0)
    b = _job("AAA", _perception("AAA", composite=50.0), 50.0)
    ranked = sr.rank_jobs([a, b])
    # equal score -> ascending coin name
    assert [j[0][0] for j in ranked] == ["AAA", "ZZZ"]


def test_multiple_fired_triggers_outrank_single_at_same_composite():
    one = _perception("ONE", composite=50.0, triggers=[
        _trigger("breakout", fired=True, score=6.0)])
    three = _perception("THREE", composite=50.0, triggers=[
        _trigger("breakout", fired=True, score=6.0),
        _trigger("momentumBurst", fired=True, score=6.0),
        _trigger("volumeBuildup1h", fired=True, score=6.0)])
    assert sr.rank_score(three, 50.0) > sr.rank_score(one, 50.0)


def test_whale_signal_boosts_score():
    base = _perception("AAA", composite=50.0)
    whale = _perception("AAA", composite=50.0, whale=True)
    assert sr.rank_score(whale, 50.0) > sr.rank_score(base, 50.0)


def test_hip3_market_penalised_vs_main_universe():
    main = _perception("AAA", composite=50.0, ptype="crypto")
    hip = _perception("HHH", composite=50.0, ptype="HIP-3")
    assert sr.rank_score(main, 50.0) > sr.rank_score(hip, 50.0)


def test_cvd_strength_normalises_fractional_and_percent():
    frac = _perception("AAA", composite=50.0,
                       cvd={"strength_pct": 0.5})
    pct = _perception("AAA", composite=50.0,
                      cvd={"strength_pct": 50.0})
    assert sr.rank_score(frac, 50.0) == pytest.approx(sr.rank_score(pct, 50.0))


def test_select_top_jobs_splits_order():
    jobs = [
        _job(c, _perception(c, composite=float(v)), float(v))
        for c, v in [("A", 10), ("B", 90), ("C", 50), ("D", 70)]]
    selected, deferred = sr.select_top_jobs(jobs, 2)
    selected_coins = [j[0] for j in selected]
    assert selected_coins == ["B", "D"]
    # deferred worst-first, covers the remainder
    deferred_coins = [j[0][0] for j in deferred]
    assert set(deferred_coins) == {"A", "C"}
    assert deferred_coins[0] == "A"


def test_select_top_jobs_zero_and_overflow():
    jobs = [_job("A", _perception("A"), 50.0),
            _job("B", _perception("B"), 50.0)]
    selected, deferred = sr.select_top_jobs(jobs, 0)
    assert selected == [] and len(deferred) == 2
    selected, deferred = sr.select_top_jobs(jobs, 10)
    assert len(selected) == 2 and deferred == []


def test_custom_weights_respected_and_normalised():
    p = _perception("AAA", composite=50.0)
    # Zeroing every weight except composite -> score tracks composite only.
    w = {"composite": 1.0, "trigger_quality": 0, "whale": 0,
         "cvd": 0, "liquidity": 0, "class_penalty": 0}
    s = sr.rank_score(p, 40.0, weights=w)
    assert s == pytest.approx(40.0)


def test_bullish_fired_count_and_conjunction_view():
    # Names use the raw perception trigger naming (camelCase), unlike the
    # _trigger helper used elsewhere which is fine with arbitrary names.
    p = _perception("AAA", composite=50.0, triggers=[
        {"name": "breakout", "fired": True, "score": 6},
        {"name": "momentumBurst", "fired": True, "score": 5},
        {"name": "downtrendMomentum", "fired": True, "score": 5},
        {"name": "dailyMover", "fired": False, "score": 0}])
    # downtrendMomentum is not in the bullish set -> 2 bullish fired
    assert sr.bullish_fired_count(p) == 2
    view = sr.conjunction_view(p)
    assert view["bullish_fired"] == 2
    assert view["passes_and"] == {"2": True, "3": False}


def test_conjunction_view_single_trigger_fails_and2():
    p = _perception("AAA")
    view = sr.conjunction_view(p)
    assert view["bullish_fired"] == 1
    assert view["passes_and"]["2"] is False


def test_garbage_fields_do_not_raise():
    p = _perception("AAA", composite="nonsense")
    p["triggers"] = [{"fired": "x", "score": None}]
    p["cvd_divergence"] = {"strength_pct": "bad"}
    # Must not raise; falls back to safe zero/neutral components.
    s = sr.rank_score(p, None)
    assert 0.0 <= s <= 100.0
