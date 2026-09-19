"""Point-in-time event-driven backtest driver.

Loop shape (single position, no same-bar re-entry):

* At the CLOSE of bar ``i`` an entry :class:`Signal` may fire; it fills at the
  OPEN of bar ``i+1`` (a strategy cannot trade on a bar before that bar's data
  exists).
* While in a position, bars are fed to the exit adapter (production
  DSLTracker). An exit on bar ``j`` fills on that same bar via the adapter's
  reference price plus exit slippage; a new signal may only act on ``j+1``.
* An open position at the last bar closes at that bar's close (end_of_data).

This is deliberately a long/short single-position kernel: portfolio stacking,
multi-asset scheduling and parameter sweeps layer on top later (P4-4/P6).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from hermes_trader.models.types import Candle

from .cost import CostModel
from .exit_dsl import BAR_MS_5M, DslBarExit
from .types import ExitEvent, ExitReason, Side, Signal, Trade


def run(
    bars: Sequence[Candle],
    signals: Sequence[Signal],
    policy,
    *,
    coin: str = "BACKTEST",
    leverage: int = 1,
    notional_usd: float = 10_000.0,
    cost: Optional[CostModel] = None,
    entry_atr_pct: float = 0.0,
    entry_regime: str = "",
    bar_ms: int = BAR_MS_5M,
) -> list[Trade]:
    """Run one-coin backtest; return completed trades in chronological order.

    ``bar_ms`` is the bars' real interval; it only calibrates the production
    exit engine's wall-clock timeouts (hard timeout / stale-flat), which are
    measured in minutes rather than bars. It defaults to the 5m live cadence.
    """
    cost = cost or CostModel()
    if not bars:
        return []
    by_index = sorted(signals, key=lambda s: s.bar_index)
    pending_i = 0

    trades: list[Trade] = []
    # A signal decided at bar i-1's close, awaiting its fill at bar i's open.
    pending: Optional[Signal] = None
    exit_engine: Optional[DslBarExit] = None
    entry_side: Side = "long"
    entry_bar = 0
    entry_ref = entry_fill = 0.0

    n = len(bars)
    for i in range(n):
        bar = bars[i]

        # ── Pending entry from a prior close fills at THIS bar's open ──
        if pending is not None:
            entry_side = pending.side
            entry_ref = bar.o
            entry_fill = cost.fill_entry(bar.o, entry_side)
            entry_bar = i
            # A signal carrying its own decision-time context (replay) wins;
            # constant-context runs (heuristic) fall back to the run-level values.
            sig_atr = pending.entry_atr_pct or entry_atr_pct
            sig_regime = pending.entry_regime or entry_regime
            exit_engine = DslBarExit(
                side=entry_side, entry_px=entry_fill,
                # Candle t is the bar-OPEN ms — also the entry instant.
                entry_time_ms=bar.t, policy=policy, leverage=leverage,
                coin=coin, entry_atr_pct=sig_atr, entry_regime=sig_regime,
                bar_ms=bar_ms,
            )
            pending = None

        # ── In a position: feed THIS bar (the entry bar first, rel=0) ──
        if exit_engine is not None:
            exit_event = exit_engine.on_bar(bar, i - entry_bar)
            if exit_event is None and i == n - 1:
                exit_event = ExitEvent(i, ExitReason.END_OF_DATA, bar.c)

            if exit_event is not None:
                # The adapter indexes bars relative to the entry bar; the
                # position closes on the bar currently being processed.
                exit_bar = i
                exit_fill = cost.fill_exit(exit_event.ref_px, entry_side,
                                           exit_event.reason)
                gross, net = cost.pnl_usd(entry_side, entry_fill, exit_fill,
                                          notional_usd)
                trades.append(Trade(
                    coin=coin, side=entry_side, entry_bar=entry_bar,
                    exit_bar=exit_bar,
                    entry_time_ms=bars[entry_bar].t,
                    exit_time_ms=bars[exit_bar].t,
                    entry_ref_px=entry_ref, entry_fill_px=entry_fill,
                    exit_ref_px=exit_event.ref_px, exit_fill_px=exit_fill,
                    reason=exit_event.reason, notional_usd=notional_usd,
                    fee_usd=cost.fee_usd(notional_usd),
                    pnl_gross_usd=gross, pnl_net_usd=net,
                ))
                exit_engine = None
                # A new signal decided on this bar's close may still fill at
                # the NEXT bar's open (pulled below): no same-bar re-entry.

        # ── Pull the signal decided at THIS bar's close ──
        while pending_i < len(by_index) and by_index[pending_i].bar_index <= i:
            sig = by_index[pending_i]
            pending_i += 1
            # Acts only if flat and it can fill at a later bar's open.
            if sig.bar_index == i and exit_engine is None and sig.bar_index < n - 1:
                pending = sig

    return trades
