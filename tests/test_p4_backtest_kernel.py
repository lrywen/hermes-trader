"""P4-2: unit tests for the unified backtest kernel.

Three layers, independently:

  * cost contract (H-7: entry 5bps / exit 15bps / max-loss +10bps, one 5bps fee)
  * reason normalization (production verdict prose -> stable ExitReason)
  * the PIT driver end-to-end over synthetic bars, asserting it reproduces the
    PRODUCTION DSL semantics pinned in tests/test_p4_dsl_parity.py — notably:
      - s1 a stop struck within the ENTRY bar is caught (D3),
      - s4 15-minute hard timeout fires on the third bar at risk (D4),
      - s5 multi-tier selection (D6) and the s2 prior-bar peak rule (D1),
      - s7 end_of_data forced close.
"""
from __future__ import annotations

import pytest

from hermes_trader.agents.dsl_exit import ExitPolicy, RetraceTier
from hermes_trader.backtest import driver
from hermes_trader.backtest.cost import CostModel
from hermes_trader.backtest.types import (
    ExitReason,
    Signal,
    normalize_reason,
)
from hermes_trader.models.types import Candle

BAR_MS = 300_000
T0 = 1_700_000_000_000
NOTIONAL = 10_000.0

# Floors anchor to the actual fill, so tests pinning exact production parity
# prices (101.75 / 101.95 off a 100 open) run with all cost legs disabled.
ZERO_COST = CostModel(round_trip_fee_bps=0.0, entry_slip_bps=0.0,
                      exit_slip_bps=0.0, stop_delay_slip_bps=0.0)


def _bars(rows: list[tuple[float, float, float, float]],
          t0: int = T0) -> list[Candle]:
    return [Candle(t=t0 + i * BAR_MS, o=o, h=h, l=l, c=c, v=1.0)
            for i, (o, h, l, c) in enumerate(rows)]


def _policy(**kw) -> ExitPolicy:
    base = dict(
        max_loss_pct=2.5, max_loss_roe_pct=100.0, protect_pct=1.5,
        retrace_threshold=0.30, hard_timeout_minutes=1e9,
        breakeven_trigger_pct=0.0, breakeven_lock_pct=0.0,
        stale_flat_timeout_minutes=0.0,
        hard_stop_confirm_sec=0.0, breach_confirm_sec=0.0,
    )
    base.update(kw)
    return ExitPolicy(**base)


# ── Cost model ──────────────────────────────────────────────────────────────

def test_cost_entry_exit_and_fee() -> None:
    cm = CostModel()
    assert cm.fill_entry(100.0, "long") == pytest.approx(100.05)
    assert cm.fill_entry(100.0, "short") == pytest.approx(99.95)
    # Normal exit: 15bps adverse to the position (long sells lower).
    assert cm.fill_exit(100.0, "long", ExitReason.FLOOR_BREACH) == pytest.approx(99.85)
    # Covering a short is a buy.
    assert cm.fill_exit(100.0, "short", ExitReason.HARD_TIMEOUT) == pytest.approx(100.15)
    # Max-loss adds the 10bps stop-delay leg: 25bps total.
    assert cm.fill_exit(100.0, "long", ExitReason.MAX_LOSS) == pytest.approx(99.75)
    assert cm.fill_exit(100.0, "short", ExitReason.MAX_LOSS) == pytest.approx(100.25)
    assert cm.fee_usd(NOTIONAL) == pytest.approx(5.0)


def test_cost_pnl_long_and_short() -> None:
    cm = CostModel()
    gross, net = cm.pnl_usd("long", 100.0, 101.0, NOTIONAL)
    assert gross == pytest.approx(100.0)
    assert net == pytest.approx(95.0)
    gross, net = cm.pnl_usd("short", 100.0, 99.0, NOTIONAL)
    assert gross == pytest.approx(100.0)
    assert net == pytest.approx(95.0)


def test_zero_bps_disables_leg() -> None:
    cm = CostModel(round_trip_fee_bps=0.0, entry_slip_bps=0.0,
                   exit_slip_bps=0.0, stop_delay_slip_bps=0.0)
    assert cm.fill_entry(100.0, "long") == 100.0
    assert cm.fill_exit(100.0, "long", ExitReason.MAX_LOSS) == 100.0
    assert cm.fee_usd(NOTIONAL) == 0.0
    assert cm.pnl_usd("long", 100.0, 101.0, NOTIONAL) == (pytest.approx(100.0),) * 2


# ── Reason normalization ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("max_loss (2.50% spot vs -2.55% roe)", ExitReason.MAX_LOSS),
    ("floor_breach: trailing stop", ExitReason.FLOOR_BREACH),
    ("trailing_stop", ExitReason.FLOOR_BREACH),
    ("hard_timeout 1800s", ExitReason.HARD_TIMEOUT),
    ("stale_flat_timeout 480m", ExitReason.STALE_FLAT_TIMEOUT),
    ("time_scratch 60s", ExitReason.TIME_SCRATCH),
    ("end_of_window", ExitReason.END_OF_DATA),
    ("no_data", ExitReason.END_OF_DATA),
])
def test_normalize_reason(raw: str, want: ExitReason) -> None:
    assert normalize_reason(raw) is want


def test_normalize_reason_unknown_raises() -> None:
    with pytest.raises(ValueError):
        normalize_reason("mystery_exit")


# ── Driver PIT semantics ────────────────────────────────────────────────────

def test_driver_empty_inputs() -> None:
    assert driver.run([], [], _policy()) == []
    assert driver.run(_bars([(100, 100, 100, 100)]), [], _policy()) == []


def test_signal_on_last_bar_never_enters() -> None:
    bars = _bars([(100, 101, 99, 100)])
    assert driver.run(bars, [Signal(0, "long")], _policy()) == []


def test_s1_stop_out_within_entry_bar() -> None:
    """D3 spirit: the entry bar's own low is fed (rel=0), so a stop within the
    first 5 minutes is caught. With market-on-open fills the stop anchors to
    the actual 100.05 fill: 2.5% hard stop = 97.54875, low 97.0 -> max_loss.
    """
    bars = _bars([
        (100, 100, 100, 100),        # signal bar: long decided at its close
        (100.0, 100.3, 97.0, 97.2),  # entry bar: fills at open, dives -2.5%
    ])
    trades = driver.run(bars, [Signal(0, "long")], _policy(), notional_usd=NOTIONAL)
    assert len(trades) == 1
    t = trades[0]
    assert (t.entry_bar, t.exit_bar, t.reason) == (1, 1, ExitReason.MAX_LOSS)
    # Entry filled 5bps above the 100 open.
    assert t.entry_ref_px == pytest.approx(100.0)
    assert t.entry_fill_px == pytest.approx(100.05)
    # Stop floor derived from the FILL, gap-filled against the bar open.
    assert t.exit_ref_px == pytest.approx(100.05 * 0.975)
    # Exit = ref*(1 - (15+10)bps), fee 5bps charged once.
    assert t.exit_fill_px == pytest.approx(100.05 * 0.975 * 0.9975)
    assert t.fee_usd == pytest.approx(5.0)
    assert t.hold_bars == 0


def test_s4_hard_timeout_third_bar() -> None:
    """D4: 15-minute timeout fires on the third bar at risk (absolute bar 3)."""
    rows = [(100.0, 100.0, 100.0, 100.0)] * 6
    bars = _bars(rows)
    pol = _policy(protect_pct=100.0, hard_timeout_minutes=15.0)
    trades = driver.run(bars, [Signal(0, "long")], pol)
    assert len(trades) == 1
    t = trades[0]
    # Entry bar = 1; bars at risk 1,2,3 -> exit bar 3, elapsed 15 minutes.
    assert (t.entry_bar, t.exit_bar) == (1, 3)
    assert t.reason is ExitReason.HARD_TIMEOUT
    assert t.hold_bars == 2
    assert t.exit_ref_px == pytest.approx(100.0)


def test_s4_hard_timeout_honors_bar_ms_for_1h_bars() -> None:
    """P4-5: a 60-minute wall-clock timeout on 1h bars fires on the entry bar
    itself (60 wall minutes elapsed at its close), while on 5m bars the same
    policy needs twelve bars (timeouts are minutes, not bar counts)."""
    one_hour = 3_600_000
    rows = [(100.0, 100.0, 100.0, 100.0)] * 4
    bars = [Candle(t=T0 + i * one_hour, o=o, h=h, l=l, c=c, v=1.0)
            for i, (o, h, l, c) in enumerate(rows)]
    pol = _policy(protect_pct=100.0, hard_timeout_minutes=60.0)
    trades = driver.run(bars, [Signal(0, "long")], pol, bar_ms=one_hour)
    assert len(trades) == 1
    t = trades[0]
    # Entry bar = 1; its close is exactly 60 minutes after the entry open, so
    # the wall-clock timeout fires on the entry bar itself (rel 0, hold 0).
    assert (t.entry_bar, t.exit_bar) == (1, 1)
    assert t.reason is ExitReason.HARD_TIMEOUT


def test_s4_hard_timeout_without_bar_ms_treats_1h_bars_as_5m() -> None:
    """Control: with the 5m default, 1h-spaced candles only advance the clock
    5 minutes per bar, so a 60-minute timeout needs twelve virtual bars."""
    one_hour = 3_600_000
    rows = [(100.0, 100.0, 100.0, 100.0)] * 13
    bars = [Candle(t=T0 + i * one_hour, o=o, h=h, l=l, c=c, v=1.0)
            for i, (o, h, l, c) in enumerate(rows)]
    pol = _policy(protect_pct=100.0, hard_timeout_minutes=60.0)
    trades = driver.run(bars, [Signal(0, "long")], pol)
    # rel+1 = 12 virtual 5m bars -> rel 11 -> absolute exit bar 12.
    assert trades[0].exit_bar == 12


def test_s5_multi_tier_floor() -> None:
    """D1+D6: spike bar's peak arms the .35 tier; floor breached next bar."""
    bars = _bars([
        (100, 100, 100, 100),          # signal bar
        (100.0, 103.0, 99.9, 102.8),   # entry bar: peak 103, survives
        (102.8, 102.9, 101.0, 101.5),  # low breaches prior-bar floor 101.95
    ])
    pol = _policy(
        max_loss_pct=1.0, protect_pct=1.5, retrace_threshold=0.15,
        breakeven_trigger_pct=2.5, breakeven_lock_pct=0.3,
        phase2_tiers=[RetraceTier(2.0, 0.35), RetraceTier(6.0, 0.30),
                      RetraceTier(12.0, 0.20), RetraceTier(20.0, 0.15)],
    )
    trades = driver.run(bars, [Signal(0, "long")], pol, cost=ZERO_COST)
    assert len(trades) == 1
    t = trades[0]
    assert (t.entry_bar, t.exit_bar) == (1, 2)
    assert t.reason is ExitReason.FLOOR_BREACH
    assert t.exit_ref_px == pytest.approx(101.95)


def test_s2_trail_uses_prior_bar_peak() -> None:
    """D1: the floor is raised only from a PRIOR bar's peak, never same-bar."""
    bars = _bars([
        (100, 100, 100, 100),          # signal bar
        (100.0, 102.5, 99.8, 102.0),   # entry bar: peak 102.5 -> floor 101.75
        (102.0, 102.2, 101.5, 101.6),  # breaches prior-bar floor
    ])
    trades = driver.run(
        bars, [Signal(0, "long")], _policy(), cost=ZERO_COST)
    assert len(trades) == 1
    t = trades[0]
    assert (t.entry_bar, t.exit_bar, t.reason) == (1, 2, ExitReason.FLOOR_BREACH)
    assert t.exit_ref_px == pytest.approx(101.75)


def test_s7_end_of_data_forced_close() -> None:
    bars = _bars([
        (100, 100, 100, 100),       # signal bar
        (100.0, 100.05, 99.95, 100.0),
        (100.0, 100.05, 99.95, 100.0),
    ])
    trades = driver.run(bars, [Signal(0, "long")], _policy())
    assert len(trades) == 1
    t = trades[0]
    assert (t.entry_bar, t.exit_bar, t.reason) == (1, 2, ExitReason.END_OF_DATA)
    assert t.entry_time_ms == bars[1].t and t.exit_time_ms == bars[2].t


def test_short_stop_within_entry_bar() -> None:
    """D3 short mirror: entry fills at the open (99.95 after 5bps), the same
    bar's high spikes +2.5% off the fill -> max_loss, covered with slippage.
    """
    bars = _bars([
        (100, 100, 100, 100),          # signal bar
        (100.0, 103.0, 99.5, 102.5),   # entry bar: fills short at open, spikes
    ])
    trades = driver.run(bars, [Signal(0, "short")], _policy())
    assert len(trades) == 1
    t = trades[0]
    assert (t.entry_bar, t.exit_bar, t.reason) == (1, 1, ExitReason.MAX_LOSS)
    # Short entry sells 5bps below the 100 open; stop ref = fill * 1.025.
    assert t.entry_fill_px == pytest.approx(99.95)
    assert t.exit_ref_px == pytest.approx(99.95 * 1.025)
    # Covering a buy pays the 15bps exit + 10bps stop-delay leg.
    assert t.exit_fill_px == pytest.approx(99.95 * 1.025 * 1.0025)


def test_no_same_bar_reentry_after_exit() -> None:
    """A signal on an exit bar's close fills only at the NEXT bar's open."""
    bars = _bars([
        (100, 100, 100, 100),          # bar 0: signal
        (100.0, 100.3, 97.0, 97.2),    # bar 1: enter at open, stopped same bar
        (100.0, 100.0, 100.0, 100.0),  # bar 1 close signal -> enter here
        (100.0, 100.05, 99.95, 100.0),
        (100.0, 100.05, 99.95, 100.0),
    ])
    trades = driver.run(bars, [Signal(0, "long"), Signal(1, "long")], _policy())
    assert [x.entry_bar for x in trades] == [1, 2]
    assert trades[0].exit_bar == 1
    assert trades[1].reason is ExitReason.END_OF_DATA
