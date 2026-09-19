"""P4-4: structural point-in-time guard tests.

These pin the no-look-ahead invariants the kernel relies on:
signals decide on a closed bar and fill next open, last-bar signals never
trade, same-bar duplicate signals are rejected, trades never exit before
entry, positions never overlap, and recorded timestamps match the bars.
"""
from __future__ import annotations

import pytest

from hermes_trader.backtest import driver, guard
from hermes_trader.backtest.cost import CostModel
from hermes_trader.backtest.types import ExitReason, Signal, Trade
from hermes_trader.models.types import Candle

BAR_MS = 300_000
T0 = 1_700_000_000_000

ZERO_COST = CostModel(round_trip_fee_bps=0.0, entry_slip_bps=0.0,
                      exit_slip_bps=0.0, stop_delay_slip_bps=0.0)


def _bars(n: int, *, t0: int = T0, px: float = 100.0) -> list[Candle]:
    return [Candle(t=t0 + i * BAR_MS, o=px, h=px + 1, l=px - 1, c=px, v=1.0)
            for i in range(n)]


def _trade(
    *,
    coin: str = "BTC",
    entry_bar: int = 1,
    exit_bar: int = 2,
    entry_t: int = T0 + BAR_MS,
    exit_t: int = T0 + 2 * BAR_MS,
    reason: ExitReason = ExitReason.HARD_TIMEOUT,
    pnl: float = 10.0,
) -> Trade:
    return Trade(
        coin=coin, side="long", entry_bar=entry_bar, exit_bar=exit_bar,
        entry_time_ms=entry_t, exit_time_ms=exit_t,
        entry_ref_px=100.0, entry_fill_px=100.0,
        exit_ref_px=100.0, exit_fill_px=100.0,
        reason=reason, notional_usd=10_000.0, fee_usd=0.0,
        pnl_gross_usd=pnl, pnl_net_usd=pnl,
    )


def _policy() -> object:
    from hermes_trader.agents.dsl_exit import ExitPolicy
    return ExitPolicy(
        max_loss_pct=2.5, max_loss_roe_pct=100.0, protect_pct=1.5,
        retrace_threshold=0.30, hard_timeout_minutes=1e9,
        breakeven_trigger_pct=0.0, breakeven_lock_pct=0.0,
        stale_flat_timeout_minutes=0.0,
        hard_stop_confirm_sec=0.0, breach_confirm_sec=0.0,
    )


# ── signal guards ───────────────────────────────────────────────────────────

def test_clean_signals_pass() -> None:
    sigs = [Signal(0, "long"), Signal(50, "short"), Signal(98, "long")]
    assert guard.check_signals_pit(sigs, n_bars=100) == []


def test_signal_on_last_bar_is_lookahead() -> None:
    errors = guard.check_signals_pit([Signal(99, "long")], n_bars=100)
    assert len(errors) == 1 and "last tradeable" in errors[0]


def test_signal_beyond_bars_rejected() -> None:
    errors = guard.check_signals_pit([Signal(140, "long")], n_bars=100)
    assert errors and "last tradeable" in errors[0]


def test_negative_bar_signal_rejected() -> None:
    errors = guard.check_signals_pit([Signal(-1, "long")], n_bars=100)
    assert errors and "invalid decision bar_index" in errors[0]


def test_invalid_side_reported() -> None:
    errors = guard.check_signals_pit([Signal(10, "banana")], n_bars=100)  # type: ignore[list-item]
    assert errors and "invalid side" in errors[0]


def test_duplicate_decision_bar_rejected() -> None:
    sigs = [Signal(10, "long"), Signal(10, "short")]
    errors = guard.check_signals_pit(sigs, n_bars=100)
    assert any("duplicate signal" in e for e in errors)


def test_empty_signals_clean_even_without_bars() -> None:
    assert guard.check_signals_pit([], n_bars=0) == []


def test_assert_raises_with_all_violations_listed() -> None:
    sigs = [Signal(99, "long"), Signal(-2, "nope"), Signal(99, "short")]  # type: ignore[list-item]
    with pytest.raises(AssertionError) as exc:
        guard.assert_signals_pit(sigs, n_bars=100)
    msg = str(exc.value)
    assert msg.count("signal[") >= 3


# ── trade guards ────────────────────────────────────────────────────────────

def test_clean_trades_pass_with_bars() -> None:
    bars = _bars(10)
    trades = [_trade(entry_bar=1, exit_bar=2),
              _trade(entry_bar=3, exit_bar=4,
                     entry_t=T0 + 3 * BAR_MS, exit_t=T0 + 4 * BAR_MS)]
    assert guard.check_trades_pit(trades, bars=bars) == []


def test_exit_before_entry_bar_rejected() -> None:
    tr = _trade(entry_bar=5, exit_bar=4)
    assert guard.check_trades_pit([tr]) and "before entry" in guard.check_trades_pit([tr])[0]


def test_intrabar_exit_entry_bar_equals_exit_bar_is_legal() -> None:
    bars = _bars(5)
    tr = _trade(entry_bar=1, exit_bar=1, entry_t=T0 + BAR_MS, exit_t=T0 + BAR_MS)
    assert guard.check_trades_pit([tr], bars=bars) == []


def test_exit_time_before_entry_time_rejected() -> None:
    tr = _trade(entry_bar=1, exit_bar=2, entry_t=T0 + 2 * BAR_MS, exit_t=T0 + BAR_MS)
    errors = guard.check_trades_pit([tr])
    assert errors and "precedes entry_time" in errors[0]


def test_timestamp_must_match_bar_open() -> None:
    bars = _bars(5)
    tr = _trade(entry_bar=1, exit_bar=2, entry_t=T0 + BAR_MS, exit_t=T0 + 999)
    errors = guard.check_trades_pit([tr], bars=bars)
    assert any("exit_time" in e and "open time" in e for e in errors)


def test_bar_index_out_of_range_rejected() -> None:
    bars = _bars(3)
    tr = _trade(entry_bar=1, exit_bar=9, entry_t=T0 + BAR_MS,
                exit_t=T0 + 9 * BAR_MS)
    errors = guard.check_trades_pit([tr], bars=bars)
    assert errors and "out of range" in errors[0]


def test_overlapping_trades_rejected_even_when_unordered() -> None:
    # Fed newest-first; the guard sorts internally and still detects overlap.
    later = _trade(entry_bar=3, exit_bar=5, entry_t=T0 + 3 * BAR_MS,
                   exit_t=T0 + 5 * BAR_MS)
    earlier = _trade(entry_bar=1, exit_bar=4)
    errors = guard.check_trades_pit([later, earlier])
    assert errors and "overlapping trades" in errors[0]


def test_back_to_back_trades_touching_bar_boundary_are_legal() -> None:
    trades = [
        _trade(entry_bar=1, exit_bar=2),
        _trade(entry_bar=3, exit_bar=3, entry_t=T0 + 3 * BAR_MS,
               exit_t=T0 + 3 * BAR_MS),
    ]
    assert guard.check_trades_pit(trades) == []


# ── end-to-end run guard ────────────────────────────────────────────────────

def test_assert_run_pit_accepts_real_kernel_output() -> None:
    bars = _bars(20, px=100.0)
    policy = _policy()
    signals = [Signal(0, "long")]
    trades = driver.run(bars, signals, policy, cost=ZERO_COST)
    assert trades, "synthetic run should produce at least one trade"
    guard.assert_run_pit(bars, signals, trades)  # no raise


def test_assert_run_pit_flags_undecidable_last_bar_signal() -> None:
    bars = _bars(5)
    policy = _policy()
    signals = [Signal(4, "long")]
    trades = driver.run(bars, signals, policy, cost=ZERO_COST)
    assert trades == []  # driver silently drops it
    with pytest.raises(AssertionError, match="last tradeable"):
        guard.assert_run_pit(bars, signals, trades)


# ── signal generators must satisfy the PIT contract by construction ─────────

def _trend_bars(n: int, step: float = 0.007) -> list[Candle]:
    bars: list[Candle] = []
    px = 100.0
    for i in range(n):
        o = px
        c = o * (1 + step)
        wick = o * 0.001
        bars.append(Candle(t=T0 + i * BAR_MS, o=o, h=max(o, c) + wick,
                           l=min(o, c) - wick, c=c, v=1.0))
        px = c
    return bars


def test_heuristic_signal_output_is_pit_clean() -> None:
    from hermes_trader.backtest.signals import (
        default_heuristic_config,
        heuristic_signals,
    )

    bars = _trend_bars(260)
    signals = heuristic_signals(bars, default_heuristic_config(warmup=100))
    assert signals, "strong synthetic trend should fire at least one long"
    guard.assert_signals_pit(signals, n_bars=len(bars))
    # Decision-bar uniqueness is guaranteed by the one-signal-per-bar loop.
    indices = [s.bar_index for s in signals]
    assert indices == sorted(set(indices))


def test_replay_signal_output_is_pit_clean() -> None:
    from hermes_trader.backtest.signals import ReplayConfig, replay_signals

    n = 120
    bars = _bars(n, px=100.0)
    # AI LONG decided 1ms after bar 30 opens → first open at/after ts is bar 31,
    # so the decision bar is 30 and the order fills at bar 31's open.
    analyses = [{
        "verdict": "LONG", "confidence": 0.8, "coin": "BTC",
        "created_at": T0 + 30 * BAR_MS + 1, "perception_id": "p1",
    }]
    perceptions = {"p1": {"composite_score": 80.0, "triggers": []}}
    signals = replay_signals(
        bars, analyses, perceptions_by_id=perceptions,
        cfg=ReplayConfig(dedup_ms=BAR_MS), coin="BTC",
    )
    assert len(signals) == 1
    guard.assert_signals_pit(signals, n_bars=n)
