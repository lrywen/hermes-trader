"""Portfolio-level scheduling across coins (B-2: live max-concurrent cap).

The per-coin :func:`hermes_trader.backtest.driver.run` is a single-position
replay: it never holds two trades on the SAME coin at once. Production, though,
runs many coins with a single shared ceiling on how many positions may be open
simultaneously (:data:`hermes_trader.backtest.guard.MAX_CONCURRENT_POSITIONS`,
the live executor's real cap = 2).

Legacy research paths applied this cap inconsistently (a post-hoc
``_apply_maxc`` that defaulted to "no cap", letting peak concurrency reach 7),
which overstated capacity: P3-4 measured the filter arm at +13.86% uncapped vs
-5.20% once the live cap of 2 is applied — a 19-point swing. This module is the
single scheduler every backtest path must funnel cross-coin trades through.

Scheduling semantics (mirror the production slot model):

* trades are considered in chronological entry order;
* a position occupies one slot for the half-open interval
  ``[entry_time_ms, exit_time_ms)`` — a new entry at exactly the prior exit
  instant reuses the freed slot;
* when every slot is occupied the incoming signal is DROPPED (the live executor
  simply does not open a new position);
* the ceiling is validated through the kernel guard, so a caller cannot quietly
  request a higher-than-live concurrency.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from .guard import MAX_CONCURRENT_POSITIONS, assert_max_concurrent_allowed
from .types import Trade


def apply_max_concurrent(
    trades: Sequence[Trade],
    max_concurrent: int = MAX_CONCURRENT_POSITIONS,
) -> list[Trade]:
    """Return the trades that fill under the live cross-coin position cap.

    Ties on ``entry_time_ms`` keep the input's relative order (``sorted`` is
    stable), which is deterministic for a given assembled trade list.
    """
    assert_max_concurrent_allowed(max_concurrent)
    ordered = sorted(trades, key=lambda t: t.entry_time_ms)
    open_exits: list[int] = []
    kept: list[Trade] = []
    for tr in ordered:
        # A slot frees at the prior position's exit instant; an entry at exactly
        # that time may reuse it (half-open occupancy interval).
        open_exits = [e for e in open_exits if e > tr.entry_time_ms]
        if len(open_exits) >= max_concurrent:
            continue
        kept.append(tr)
        open_exits.append(tr.exit_time_ms)
    return kept


def peak_concurrency(trades: Iterable[Trade]) -> int:
    """Largest number of positions simultaneously open across ``trades``.

    Sweep entry/exit events in time order; exits at time ``t`` are counted
    before entries at the same ``t`` (half-open intervals touch without overlap).
    """
    events: list[tuple[int, int]] = []
    for tr in trades:
        events.append((tr.entry_time_ms, +1))
        events.append((tr.exit_time_ms, -1))
    # -1 (exit) sorts before +1 (entry) at the same timestamp.
    events.sort(key=lambda ev: (ev[0], ev[1]))
    cur = peak = 0
    for _t, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak
