"""B-2: the live max-concurrent-positions cap (=2) is a kernel hard error.

Covers:

* ``guard.assert_max_concurrent_allowed`` rejects >2 (and non-positive / bool),
* ``portfolio.apply_max_concurrent`` keeps the trades that fill under 2 slots
  using half-open occupancy (an entry at the prior exit instant reuses a slot)
  and drops overflow signals in chronological order,
* ``portfolio.peak_concurrency`` measures simultaneous open positions.
"""
from __future__ import annotations

import pytest

from hermes_trader.backtest import portfolio
from hermes_trader.backtest.guard import MAX_CONCURRENT_POSITIONS
from hermes_trader.backtest.types import ExitReason, Trade


def _tr(coin: str, t0: int, t1: int, px: float = 100.0) -> Trade:
    return Trade(coin=coin, side="long", entry_bar=0, exit_bar=1,
                 entry_time_ms=t0, exit_time_ms=t1,
                 entry_ref_px=px, entry_fill_px=px,
                 exit_ref_px=px, exit_fill_px=px,
                 reason=ExitReason.END_OF_DATA, notional_usd=10_000.0,
                 fee_usd=0.0, pnl_gross_usd=0.0, pnl_net_usd=0.0)


# ── guard ──────────────────────────────────────────────────────────────────

def test_live_cap_is_two():
    assert MAX_CONCURRENT_POSITIONS == 2


@pytest.mark.parametrize("n", [1, 2])
def test_cap_at_or_below_two_allowed(n):
    portfolio.assert_max_concurrent_allowed(n)  # no raise


@pytest.mark.parametrize("n", [3, 7, 10])
def test_cap_above_two_hard_rejected(n):
    with pytest.raises(ValueError, match="max_concurrent"):
        portfolio.assert_max_concurrent_allowed(n)


@pytest.mark.parametrize("bad", [0, -1, True, False, 2.0, "2"])
def test_cap_rejects_non_positive_int(bad):
    with pytest.raises((ValueError, TypeError)):
        portfolio.assert_max_concurrent_allowed(bad)


# ── apply_max_concurrent ───────────────────────────────────────────────────

def test_default_uses_live_cap():
    # Three fully overlapping trades: only two slots -> the third is dropped.
    trades = [_tr("AAA", 0, 10), _tr("BBB", 1, 11), _tr("CCC", 2, 12)]
    kept = portfolio.apply_max_concurrent(trades)
    assert {t.coin for t in kept} == {"AAA", "BBB"}


def test_non_overlapping_all_fill_under_cap():
    trades = [_tr("AAA", 0, 5), _tr("BBB", 6, 10), _tr("CCC", 11, 15)]
    kept = portfolio.apply_max_concurrent(trades)
    assert len(kept) == 3


def test_touching_intervals_reuse_slot():
    # BBB enters at exactly AAA's exit instant -> half-open, slot is reused.
    trades = [_tr("AAA", 0, 5), _tr("BBB", 5, 10)]
    kept = portfolio.apply_max_concurrent(trades, max_concurrent=1)
    assert {t.coin for t in kept} == {"AAA", "BBB"}


def test_chronological_order_independent_of_input():
    # Supplied out of order; the earliest two fill, the late-overflowing drops.
    trades = [_tr("CCC", 2, 12), _tr("AAA", 0, 10), _tr("BBB", 1, 11)]
    kept = portfolio.apply_max_concurrent(trades)
    assert {t.coin for t in kept} == {"AAA", "BBB"}


def test_apply_rejects_above_cap():
    with pytest.raises(ValueError, match="live cap"):
        portfolio.apply_max_concurrent([_tr("AAA", 0, 1)], max_concurrent=3)


# ── peak_concurrency ───────────────────────────────────────────────────────

def test_peak_concurrency_counts_overlap():
    trades = [_tr("AAA", 0, 10), _tr("BBB", 1, 11), _tr("CCC", 5, 6)]
    assert portfolio.peak_concurrency(trades) == 3


def test_peak_concurrency_touch_not_overlap():
    trades = [_tr("AAA", 0, 5), _tr("BBB", 5, 10)]
    assert portfolio.peak_concurrency(trades) == 1
