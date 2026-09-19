"""Structural point-in-time (PIT) guards for the backtest kernel.

The kernel's no-look-ahead guarantee rests on a handful of invariants that are
easy to break silently when wiring new signal sources or replay adapters:

  * a signal decided at bar ``i``'s close can only fill at ``i+1``'s open, so
    signals on the last bar can never trade and negative indices are invalid;
  * two signals on the same decision bar are ambiguous — the driver silently
    keeps one — so they are rejected rather than ignored;
  * a trade's exit may never precede its entry (an exit INSIDE the entry bar is
    legal — ``exit_bar == entry_bar`` — since the entry bar is fed at rel=0);
  * the single-position kernel never holds overlapping trades: the next entry
    fills strictly after the prior exit bar;
  * the timestamps recorded on a trade must match the bars actually entered and
    exited (checked when ``bars`` are supplied).

These are structural assertions over kernel inputs/outputs — they do not
verify TA math, only that nothing could have peeked at future bars.
"""
from __future__ import annotations

from collections.abc import Sequence

from hermes_trader.models.types import Candle

from .types import Side, Signal, Trade

_SIDES: tuple[Side, ...] = ("long", "short")


def check_signals_pit(signals: Sequence[Signal], *, n_bars: int) -> list[str]:
    """Return a list of PIT violations in ``signals``; empty means clean."""
    errors: list[str] = []
    if n_bars <= 0:
        return errors

    seen: set[int] = set()
    for k, sig in enumerate(signals):
        idx = sig.bar_index
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            errors.append(f"signal[{k}] has invalid decision bar_index={idx!r}")
            continue
        if sig.side not in _SIDES:
            errors.append(f"signal[{k}] at bar {idx} has invalid side={sig.side!r}")
        if idx > n_bars - 2:
            # Decided on the last bar (or beyond): no later open can fill it.
            errors.append(
                f"signal[{k}] decides at bar {idx} but last tradeable "
                f"decision bar is {n_bars - 2} (n_bars={n_bars})"
            )
        if idx in seen:
            errors.append(f"duplicate signal at decision bar {idx}")
        seen.add(idx)
    return errors


def assert_signals_pit(signals: Sequence[Signal], *, n_bars: int) -> None:
    """Raise ``AssertionError`` listing every signal-side PIT violation."""
    errors = check_signals_pit(signals, n_bars=n_bars)
    if errors:
        raise AssertionError("PIT signal violations:\n  - " + "\n  - ".join(errors))


def check_trades_pit(
    trades: Sequence[Trade], *, bars: Sequence[Candle] | None = None
) -> list[str]:
    """Return a list of PIT violations in completed ``trades``."""
    errors: list[str] = []
    n = len(bars) if bars is not None else None

    for k, tr in enumerate(trades):
        if tr.entry_bar < 0 or tr.exit_bar < 0:
            errors.append(f"trade[{k}] {tr.coin} has negative bar index")
        if tr.exit_bar < tr.entry_bar:
            errors.append(
                f"trade[{k}] {tr.coin} exits at bar {tr.exit_bar} "
                f"before entry at bar {tr.entry_bar}"
            )
        if tr.exit_time_ms < tr.entry_time_ms:
            errors.append(
                f"trade[{k}] {tr.coin} exit_time {tr.exit_time_ms} "
                f"precedes entry_time {tr.entry_time_ms}"
            )
        if n is not None:
            if not 0 <= tr.entry_bar < n or not 0 <= tr.exit_bar < n:
                errors.append(
                    f"trade[{k}] {tr.coin} bar index out of range "
                    f"(entry={tr.entry_bar}, exit={tr.exit_bar}, n={n})"
                )
                continue
            if bars[tr.entry_bar].t != tr.entry_time_ms:
                errors.append(
                    f"trade[{k}] {tr.coin} entry_time {tr.entry_time_ms} != "
                    f"bar {tr.entry_bar} open time {bars[tr.entry_bar].t}"
                )
            if bars[tr.exit_bar].t != tr.exit_time_ms:
                errors.append(
                    f"trade[{k}] {tr.coin} exit_time {tr.exit_time_ms} != "
                    f"bar {tr.exit_bar} open time {bars[tr.exit_bar].t}"
                )

    # Single-position kernel: entries must be strictly ordered after exits.
    ordered = sorted(enumerate(trades), key=lambda kv: (kv[1].entry_bar, kv[0]))
    for (ki, prev), (kj, nxt) in zip(ordered, ordered[1:]):
        if nxt.entry_bar <= prev.exit_bar:
            errors.append(
                f"overlapping trades: trade[{ki}] entered bar {prev.entry_bar} "
                f"exits bar {prev.exit_bar}, but trade[{kj}] already enters "
                f"bar {nxt.entry_bar}"
            )
    return errors


def assert_trades_pit(
    trades: Sequence[Trade], *, bars: Sequence[Candle] | None = None
) -> None:
    """Raise ``AssertionError`` listing every trade-side PIT violation."""
    errors = check_trades_pit(trades, bars=bars)
    if errors:
        raise AssertionError("PIT trade violations:\n  - " + "\n  - ".join(errors))


def check_run_pit(
    bars: Sequence[Candle], signals: Sequence[Signal], trades: Sequence[Trade]
) -> list[str]:
    """Validate one complete kernel run end to end."""
    errors = check_signals_pit(signals, n_bars=len(bars))
    errors.extend(check_trades_pit(trades, bars=bars))
    return errors


def assert_run_pit(
    bars: Sequence[Candle], signals: Sequence[Signal], trades: Sequence[Trade]
) -> None:
    """Raise ``AssertionError`` listing every PIT violation in a kernel run."""
    errors = check_run_pit(bars, signals, trades)
    if errors:
        raise AssertionError("PIT run violations:\n  - " + "\n  - ".join(errors))
