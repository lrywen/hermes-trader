"""Cross-stage priority contract (A4).

Pins the admission/ordering split between the two selection stages so they can
never drift into producing coins the upstream stage did not admit:

  * coin_selection (ADMISSION): ``top_k`` output must be a subset of the
    eligible pool (and never invent a coin).
  * signal_ranking (ORDERING): ``select_top_jobs`` selected jobs must be a
    subset of the input jobs; the chosen coins must all come from the admitted
    set they were built from.

These are pure-function subset invariants; together with the fixed enforce
order documented in scripts/trading_loop.py they guarantee signal_ranking only
ever orders coins coin_selection admitted.
"""
from __future__ import annotations

from hermes_trader.agents import signal_rank as sr
from hermes_trader.agents import coin_select as cs


def _market(coin: str) -> dict:
    return {"coin": coin}


# ── coin_selection: admission subset ────────────────────────────────────────

def test_coin_selection_top_k_subset_of_eligible():
    eligible = [_market(c) for c in ("AAA", "BBB", "CCC", "DDD")]
    pick = cs.top_k(eligible, 2)
    picked_coins = {m["coin"] for m in pick}
    assert picked_coins <= {m["coin"] for m in eligible}
    assert len(pick) == 2


def test_coin_selection_k_larger_returns_all_eligible():
    eligible = [_market(c) for c in ("AAA", "BBB")]
    pick = cs.top_k(eligible, 10)
    assert {m["coin"] for m in pick} == {"AAA", "BBB"}


def test_coin_selection_zero_k_returns_none():
    eligible = [_market("AAA")]
    assert cs.top_k(eligible, 0) == []


# ── signal_ranking: ordering subset ─────────────────────────────────────────

def _job(coin: str):
    perception = {"coin": coin, "triggers": [], "composite_score": 50.0}
    return (coin, perception, 50.0, {"pass": True})


def test_signal_ranking_selected_subset_of_input_jobs():
    jobs = [_job(c) for c in ("AAA", "BBB", "CCC", "DDD")]
    selected, deferred = sr.select_top_jobs(jobs, 2)
    selected_coins = {j[0] for j in selected}
    deferred_coins = {j[0][0] for j in deferred}
    input_coins = {j[0] for j in jobs}
    assert selected_coins <= input_coins
    assert deferred_coins <= input_coins
    # Selection is a partition: disjoint, and union covers every input coin.
    assert selected_coins.isdisjoint(deferred_coins)
    assert selected_coins | deferred_coins == input_coins
    assert len(selected) == 2


def test_signal_ranking_selected_coins_come_from_admitted_set():
    # Build the admitted set first (as the live order does), then rank only
    # jobs whose coin is in it.
    admitted = {_market(c)["coin"] for c in ("AAA", "BBB", "CCC")}
    jobs = [_job(c) for c in admitted]
    selected, _ = sr.select_top_jobs(jobs, 2)
    assert {j[0] for j in selected} <= admitted
