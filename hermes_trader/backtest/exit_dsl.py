"""Point-in-time bar adapter around the PRODUCTION DSLTracker.

This is the linchpin of P4: instead of re-implementing DSL exit semantics in
the backtest (the three research scripts each did, and drifted — see
tests/test_p4_dsl_parity.py D1-D6), the kernel drives the exact exit engine
live trading uses, with bars fed through the conservative intrabar rule
already proven in the parity tests (``run_production``):

1. Both clocks are frozen to the SAME virtual instant for the bar's two checks
   (wall clock -> timeouts/hold; monotonic -> confirm gates), then restored.
2. The ADVERSE extreme is checked first (its verdict can exit); then the
   FAVORABLE extreme is checked only to advance the peak, whose verdict is
   discarded. This keeps a stop decided on a bar from using that same bar's
   favorable high to raise the floor (the D1 intrabar look-ahead).
3. Stop/floor exits fill at the floor reference gap-filled against the bar
   open: ``min(floor, open)`` long / ``max(floor, open)`` short. Timeouts have
   no floor and fill at the bar close.

Entry semantics: the position opens at the OPEN of the FIRST bar handed to the
adapter (``bar_index`` 0) — that bar IS the entry bar, and its high/low are the
first prices the live stop would see (a gap-through on the entry bar must be
caught, scenario D3/s1). Candle ``t`` is the bar-OPEN ms; that bar's close is
one bar interval after entry, so the virtual clock stamps
``entry + (bar_index+1)*bar_ms``. With 5-minute bars a 15-minute hard timeout
therefore fires at ``bar_index`` 2 (that bar's close is exactly 15 minutes
after the entry open). The bar cadence is configurable via ``bar_ms`` so
non-5m backtests still measure timeouts in real minutes (the production DSL
cadence is fixed wall-clock time, not a bar count).

Clock injection note: production ``check()`` reads module-level ``time``; until
P1 adds an explicit clock seam we freeze ``time.time``/``time.monotonic``
process-wide ONLY inside the two synchronous ``check()`` calls and restore them
in ``finally``. Backtests run in a dedicated single-threaded process.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, replace
from typing import Optional

from hermes_trader.agents import dsl_exit as dx
from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy

from .types import ExitEvent, Side, normalize_reason

#: Default bar cadence: 5-minute bars, the production DSL cadence.
BAR_MS_5M = 300_000


@contextlib.contextmanager
def _frozen_clock(wall: float, mono: float):
    """Temporarily pin time.time/monotonic; restored even if check() raises."""
    real_time, real_mono = time.time, time.monotonic
    time.time = lambda: wall
    time.monotonic = lambda: mono
    try:
        yield
    finally:
        time.time = real_time
        time.monotonic = real_mono


@contextlib.contextmanager
def _no_persistence():
    """Suppress dsl_exit's registry writes and shadow-log side effects.

    Direct-constructed trackers are never in ``_active_positions`` (a save
    would only rewrite an empty registry), but forcing no-ops still removes
    file-lock contention and the stop-tuning shadow's config read / jsonl
    append during a fast backtest. Best-effort: restore everything on exit.
    """
    real_save = dx._request_save
    real_shadow = dx._record_stop_tuning_shadow
    dx._request_save = lambda force=False: None
    dx._record_stop_tuning_shadow = lambda *a, **k: None
    try:
        yield
    finally:
        dx._request_save = real_save
        dx._record_stop_tuning_shadow = real_shadow


@dataclass
class DslBarExit:
    """Exit adapter: feed bars of a fixed cadence; get the first :class:`ExitEvent`.

    Parameters mirror DSLTracker. Confirmation gates are zeroed here (a bar is
    already an aggregated, confirmed candle — there is no sub-bar tick stream
    to wait on). ``bar_ms`` defaults to the production 5-minute cadence; pass
    the real interval for 15m/1h/4h/1d backtests so wall-clock timeouts fire on
    schedule.
    """

    side: Side
    entry_px: float
    entry_time_ms: int
    policy: ExitPolicy
    leverage: int = 1
    coin: str = "BACKTEST"
    entry_atr_pct: float = 0.0
    entry_regime: str = ""
    bar_ms: int = BAR_MS_5M

    def __post_init__(self) -> None:
        # Copy the policy and zero the sub-bar confirmation gates: a bar is an
        # already-aggregated, confirmed candle, so there is no tick stream to
        # wait on. Copying (not mutating the caller's policy) also detaches the
        # tiers list so a backtest can never mutate a shared live policy.
        policy = replace(self.policy, phase2_tiers=list(self.policy.phase2_tiers),
                         breach_confirm_sec=0.0, hard_stop_confirm_sec=0.0)
        self._tr = DSLTracker(
            self.coin, self.side, self.entry_px, self.entry_time_ms / 1000.0,
            policy=policy, leverage=self.leverage,
            entry_atr_pct=self.entry_atr_pct, entry_regime=self.entry_regime,
        )

    def on_bar(self, bar, bar_index: int) -> Optional[ExitEvent]:
        """Process one bar (0 = the entry bar itself); exit or None.

        The entry bar is fed first: an adverse gap/open there can stop the
        position out within its first bar. Decisions are stamped at the
        bar CLOSE, ``entry + (bar_index+1)*bar_ms`` after the entry open, which
        keeps hard/stale timeouts measured from the entry open (with 5m bars a
        15-minute timeout fires at ``bar_index`` 2).
        """
        is_long = self.side == "long"
        # Virtual instant: this bar's close, relative to entry open. The bar
        # cadence is real wall-clock time (production timeouts are in minutes),
        # so non-5m feeds must pass their own ``bar_ms``.
        bar_secs = self.bar_ms / 1000.0
        wall = self.entry_time_ms / 1000.0 + (bar_index + 1) * bar_secs
        mono = float(bar_index + 1) * bar_secs
        with _no_persistence(), _frozen_clock(wall, mono):
            adverse = bar.l if is_long else bar.h
            verdict = self._tr.check(adverse, index_px=None)
            if verdict.exit:
                if verdict.floor_price is not None:
                    ref = (min(verdict.floor_price, bar.o) if is_long
                           else max(verdict.floor_price, bar.o))
                else:
                    ref = bar.c
                return ExitEvent(bar_index, normalize_reason(verdict.reason), ref)
            favorable = bar.h if is_long else bar.l
            self._tr.check(favorable, index_px=None)
        return None
