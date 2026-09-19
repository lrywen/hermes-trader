"""P4-4: unit tests for walk-forward IS/OOS statistics.

Covers:
  * trade_stats over empty / winning / losing synthetic trades,
  * expectancy, win rate, avg win/loss, by_reason and pnl%-of-equity,
  * max drawdown ordering by (exit_time_ms, coin),
  * Sharpe annualization from real entry/exit timestamps,
  * oos_split_index boundaries and validation,
  * split_trades classification by ENTRY bar (O-7 rule),
  * walk_forward edge_held_oos semantics.
"""
from __future__ import annotations

import math

import pytest

from hermes_trader.backtest.stats import (
    Stats,
    WalkForward,
    oos_split_index,
    split_trades,
    trade_stats,
    walk_forward,
)
from hermes_trader.backtest.types import ExitReason, Side, Trade

BAR_MS = 300_000
T0 = 1_700_000_000_000
DAY_MS = 86_400_000


def _trade(
    pnl_net: float,
    *,
    coin: str = "BTC",
    side: Side = "long",
    entry_bar: int = 0,
    exit_bar: int = 1,
    entry_t: int = T0,
    exit_t: int = T0 + BAR_MS,
    reason: ExitReason = ExitReason.FLOOR_BREACH,
    notional: float = 10_000.0,
) -> Trade:
    return Trade(
        coin=coin, side=side, entry_bar=entry_bar, exit_bar=exit_bar,
        entry_time_ms=entry_t, exit_time_ms=exit_t,
        entry_ref_px=100.0, entry_fill_px=100.0,
        exit_ref_px=100.0, exit_fill_px=100.0,
        reason=reason, notional_usd=notional, fee_usd=5.0,
        pnl_gross_usd=pnl_net + 5.0, pnl_net_usd=pnl_net,
    )


# ── trade_stats basics ──────────────────────────────────────────────────────

def test_empty_trades_returns_zero_stats() -> None:
    s = trade_stats([])
    assert isinstance(s, Stats)
    assert s.n == 0
    assert s.wins == 0 and s.losses == 0
    assert s.win_rate_pct == 0.0
    assert s.pnl_net_usd == 0.0
    assert s.expectancy_usd == 0.0
    assert s.max_dd_usd == 0.0
    assert s.sharpe == 0.0
    assert s.by_reason == {}
    assert s.pnl_pct_equity is None


def test_counts_rates_and_averages() -> None:
    trades = [
        _trade(100.0, reason=ExitReason.FLOOR_BREACH),
        _trade(200.0, reason=ExitReason.HARD_TIMEOUT),
        _trade(-50.0, reason=ExitReason.MAX_LOSS),
        _trade(-150.0, reason=ExitReason.MAX_LOSS),
    ]
    s = trade_stats(trades)
    assert s.n == 4
    assert s.wins == 2 and s.losses == 2
    assert s.win_rate_pct == pytest.approx(50.0)
    assert s.pnl_net_usd == pytest.approx(100.0)
    assert s.expectancy_usd == pytest.approx(25.0)
    assert s.avg_win_usd == pytest.approx(150.0)
    assert s.avg_loss_usd == pytest.approx(-100.0)
    assert s.by_reason == {"floor_breach": 1, "hard_timeout": 1, "max_loss": 2}


def test_all_winners_losses_zero_division() -> None:
    win = trade_stats([_trade(10.0)])
    assert win.avg_loss_usd == 0.0
    loss = trade_stats([_trade(-10.0)])
    assert loss.avg_win_usd == 0.0
    assert loss.win_rate_pct == 0.0


def test_pnl_pct_equity_only_when_equity_given() -> None:
    assert trade_stats([_trade(100.0)]).pnl_pct_equity is None
    s = trade_stats([_trade(100.0), _trade(-50.0)], equity=1_000.0)
    assert s.pnl_pct_equity == pytest.approx(5.0)
    # Zero/None equity must not divide.
    assert trade_stats([_trade(10.0)], equity=0.0).pnl_pct_equity is None


# ── drawdown ────────────────────────────────────────────────────────────────

def test_max_drawdown_follows_exit_order_not_input_order() -> None:
    # Fed out of chronological order; the equity curve must be rebuilt by
    # (exit_time_ms, coin): +100, then -150 (DD 150), then +50.
    trades = [
        _trade(50.0, exit_t=T0 + 3 * BAR_MS),
        _trade(-150.0, exit_t=T0 + 2 * BAR_MS),
        _trade(100.0, exit_t=T0 + 1 * BAR_MS),
    ]
    s = trade_stats(trades)
    # Path 100 -> -50 -> 0: peak 100, trough -50 → DD 150.
    assert s.max_dd_usd == pytest.approx(150.0)


def test_max_drawdown_monotonic_winnings_is_zero() -> None:
    trades = [_trade(10.0 * i, exit_t=T0 + i * BAR_MS, entry_bar=i - 1)
              for i in range(1, 6)]
    assert trade_stats(trades).max_dd_usd == pytest.approx(0.0)


def test_drawdown_tiebreaks_on_coin_deterministically() -> None:
    a = _trade(-100.0, coin="AAA", exit_t=T0 + BAR_MS)
    b = _trade(-100.0, coin="ZZZ", exit_t=T0 + BAR_MS)
    s = trade_stats([b, a])
    # Both exit at the same ms; coin tiebreak keeps a stable curve (-100,-200).
    assert s.max_dd_usd == pytest.approx(200.0)


# ── Sharpe ──────────────────────────────────────────────────────────────────

def test_single_trade_sharpe_zero_no_variance() -> None:
    assert trade_stats([_trade(100.0)]).sharpe == 0.0


def test_sharpe_annualizes_with_real_timestamps() -> None:
    # Two trades 10 days apart (entry→exit instants), pnl +100 / -100.
    trades = [
        _trade(100.0, entry_t=T0, exit_t=T0 + BAR_MS),
        _trade(-100.0, entry_t=T0 + 10 * DAY_MS, exit_t=T0 + 10 * DAY_MS + BAR_MS),
    ]
    s = trade_stats(trades)
    # Mean 0 → Sharpe 0 even with variance; build a non-zero-mean case instead.
    assert s.sharpe == pytest.approx(0.0)

    # First entry → last exit spans exactly 10 days.
    trades2 = [
        _trade(120.0, entry_t=T0, exit_t=T0 + BAR_MS),
        _trade(60.0, entry_t=T0 + 10 * DAY_MS - BAR_MS,
               exit_t=T0 + 10 * DAY_MS),
    ]
    s2 = trade_stats(trades2)
    mean = 90.0
    var = ((120 - mean) ** 2 + (60 - mean) ** 2) / (2 - 1)
    expected = mean / math.sqrt(var) * math.sqrt(365.0 / 10.0)
    assert s2.sharpe == pytest.approx(expected, rel=1e-9)


def test_sharpe_span_clamped_to_one_day() -> None:
    # Trades microseconds apart must not explode the annualization factor.
    trades = [
        _trade(120.0, entry_t=T0, exit_t=T0 + 1),
        _trade(60.0, entry_t=T0 + 2, exit_t=T0 + 3),
    ]
    s = trade_stats(trades)
    mean = 90.0
    var = 1800.0
    expected = mean / math.sqrt(var) * math.sqrt(365.0)
    assert s.sharpe == pytest.approx(expected, rel=1e-9)


# ── oos_split_index ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "n,warmup,frac,expected",
    [
        (1000, 100, 0.25, 775),   # warmup + 0.75 * 900
        (1000, 100, 0.0, 1000),  # no OOS
        (200, 200, 0.5, 200),    # n == warmup → no tradeable window
        (50, 100, 0.5, 50),      # n < warmup
        (101, 100, 0.5, 100),    # one tradeable bar, split at warmup
    ],
)
def test_oos_split_index(n: int, warmup: int, frac: float, expected: int) -> None:
    assert oos_split_index(n, warmup, frac) == expected


@pytest.mark.parametrize("frac", [-0.1, 1.0, 1.5])
def test_oos_split_index_rejects_bad_frac(frac: float) -> None:
    with pytest.raises(ValueError, match="oos_frac"):
        oos_split_index(1000, 100, frac)


# ── split_trades / walk_forward ─────────────────────────────────────────────

def test_split_trades_uses_entry_bar_not_exit_bar() -> None:
    # Entered before the split but exits after it → still in-sample (O-7).
    crossing = _trade(50.0, entry_bar=90, exit_bar=120)
    oos = _trade(-20.0, entry_bar=100, exit_bar=101)
    is_tr, oos_tr = split_trades([crossing, oos], 100)
    assert is_tr == [crossing]
    assert oos_tr == [oos]


def test_split_boundary_bar_is_oos() -> None:
    at_split = _trade(10.0, entry_bar=100, exit_bar=101)
    is_tr, oos_tr = split_trades([at_split], 100)
    assert is_tr == []
    assert oos_tr == [at_split]


def test_walk_forward_segments_and_edge_flag() -> None:
    trades = [
        _trade(100.0, entry_bar=10, exit_bar=11, entry_t=T0, exit_t=T0 + BAR_MS),
        _trade(80.0, entry_bar=20, exit_bar=21, entry_t=T0 + 2 * BAR_MS,
               exit_t=T0 + 3 * BAR_MS),
        _trade(-40.0, entry_bar=110, exit_bar=111, entry_t=T0 + 4 * BAR_MS,
               exit_t=T0 + 5 * BAR_MS),
    ]
    wf = walk_forward(trades, 100, equity=10_000.0)
    assert isinstance(wf, WalkForward)
    assert wf.split_bar == 100
    assert wf.in_sample.n == 2
    assert wf.in_sample.pnl_net_usd == pytest.approx(180.0)
    assert wf.out_of_sample.n == 1
    assert wf.out_of_sample.pnl_net_usd == pytest.approx(-40.0)
    assert wf.in_sample.pnl_pct_equity == pytest.approx(1.8)
    # Negative OOS expectancy → the edge did not hold.
    assert wf.edge_held_oos is False


def test_edge_held_requires_oos_trades_and_positive_expectancy() -> None:
    is_only = [_trade(100.0, entry_bar=10, exit_bar=11)]
    assert walk_forward(is_only, 100).edge_held_oos is False

    trades = is_only + [_trade(20.0, entry_bar=110, exit_bar=111,
                               entry_t=T0 + 10 * BAR_MS, exit_t=T0 + 11 * BAR_MS)]
    assert walk_forward(trades, 100).edge_held_oos is True
