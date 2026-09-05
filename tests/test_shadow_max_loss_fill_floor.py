"""F6: shadow/live fill parity for hard max_loss exits.

A normal live exit (hard max_loss OR trailing floor_breach) is a software IOC
market fill at the price present when the DSL signal is confirmed — that is the
same mark the shadow book observes, so the paper fill defaults to the mark.

The ONLY divergence is a gap-through: when the confirming mark has traded PAST
the live exchange backup-SL trigger (the wider disaster net resting server-
side in Phase 1), live fills at that trigger instead of the post-gap mark. The
paper book caps its fill there too. Filling max_loss blanket-style at the
tighter DSL floor (the rejected F2 model) gave the paper book a BETTER price
than live gets on every normal stop; these tests pin the F6 behaviour instead.
"""
from __future__ import annotations

from hermes_trader.agents import shadow_book as sb
from hermes_trader.agents.dsl_exit import ExitVerdict


class _FakeTracker:
    """Stand-in DSLTracker whose check() returns a scripted verdict."""

    def __init__(self, verdict):
        self._verdict = verdict

    def check(self, mark, index_px=None):
        return self._verdict


def _open_book(tmp_path, coin="BTC", side="long", entry=100.0, atr_pct=0.0):
    book = sb.ShadowBook(path=str(tmp_path / "shadow.json"))
    fill = book.shadow_open(
        coin=coin, side=side, entry_px=entry, size_usd=1000.0,
        leverage=1, entry_atr_pct=atr_pct, entry_regime="neutral",
        analysis_id="f6-test",
    )
    assert fill is not None, "shadow open should succeed in an empty book"
    return book, coin, side


def test_backup_trigger_long_clamps_to_ceiling():
    # Huge ATR (100%) * mult would put the net 150% away; the ceiling pins it
    # to 3% below entry (the post-HYPE outer-net cap).
    trig = sb._backup_sl_trigger_px(
        coin="BTC", side="long", entry_px=100.0, entry_atr_pct=100.0)
    assert trig is not None
    assert abs(trig - 97.0) < 1e-9


def test_backup_trigger_long_uses_floor():
    # Zero ATR -> the net sits at the resolved floor below entry. The canonical
    # default floor is the shared dsl_exit.atr_stop.floor_pct = 1.2%; assert the
    # floor binds (atr*mult=0 contributes nothing) and it is inside the ceiling.
    trig = sb._backup_sl_trigger_px(
        coin="BTC", side="long", entry_px=100.0, entry_atr_pct=0.0)
    assert trig is not None
    assert 96.99 < trig < 99.01        # between 3% ceiling and 1% hard default
    # A small ATR that does not clear the floor leaves the trigger unchanged.
    trig2 = sb._backup_sl_trigger_px(
        coin="BTC", side="long", entry_px=100.0, entry_atr_pct=0.1)
    assert abs(trig2 - trig) < 1e-9


def test_backup_trigger_short_is_above_entry():
    trig = sb._backup_sl_trigger_px(
        coin="ETH", side="short", entry_px=200.0, entry_atr_pct=0.0)
    assert trig is not None
    assert trig > 200.0                # short's net rests above entry
    assert 201.0 < trig < 206.01       # between 1% and 3% adverse


def test_max_loss_normal_fills_at_mark_long(tmp_path):
    # Stop trips but the confirming mark has NOT gapped past the backup net
    # (trigger at 97.0): fill at the mark, exactly like the live software IOC.
    book, coin, side = _open_book(tmp_path, entry=100.0, atr_pct=100.0)
    mark = 98.0
    floor = 99.0  # DSL stop floor — must NOT be used (F2 behaviour) on a normal exit
    book._trackers[book._key(coin, side)] = _FakeTracker(ExitVerdict(
        exit=True, reason="max_loss", floor_price=floor))

    closed = book.mark_to_market({coin: mark}, index_prices={coin: mark})

    assert len(closed) == 1
    fill = closed[0]
    assert fill["reason"] == "max_loss"
    assert abs(fill["price"] - mark) < 1e-9


def test_max_loss_gap_through_caps_at_backup_trigger_long(tmp_path):
    # Mark gapped to 94 — THROUGH the backup net resting at 97.0. Live fills at
    # the exchange trigger (97.0), not the post-gap mark nor the DSL floor.
    book, coin, side = _open_book(tmp_path, entry=100.0, atr_pct=100.0)
    mark = 94.0
    book._trackers[book._key(coin, side)] = _FakeTracker(ExitVerdict(
        exit=True, reason="max_loss", floor_price=99.0))

    closed = book.mark_to_market({coin: mark}, index_prices={coin: mark})

    assert len(closed) == 1
    fill = closed[0]
    assert abs(fill["price"] - 97.0) < 1e-9   # capped at backup trigger
    assert fill["price"] > mark                # better than the post-gap mark


def test_max_loss_gap_through_caps_at_backup_trigger_short(tmp_path):
    # Short: backup net rests 3% ABOVE entry (206.0); mark gapped to 210 past it.
    book, coin, side = _open_book(tmp_path, coin="ETH", side="short",
                                  entry=200.0, atr_pct=100.0)
    mark = 210.0
    book._trackers[book._key(coin, side)] = _FakeTracker(ExitVerdict(
        exit=True, reason="max_loss", floor_price=202.0))

    closed = book.mark_to_market({coin: mark}, index_prices={coin: mark})

    assert len(closed) == 1
    fill = closed[0]
    assert abs(fill["price"] - 206.0) < 1e-9
    assert fill["price"] < mark


def test_non_max_loss_exit_always_fills_at_mark(tmp_path):
    # A trailing/breach verdict never uses the backup net: it fills at the mark
    # even if the mark is far past where a net would sit.
    book, coin, side = _open_book(tmp_path, entry=100.0, atr_pct=100.0)
    mark = 103.0
    book._trackers[book._key(coin, side)] = _FakeTracker(ExitVerdict(
        exit=True, reason="trailing_stop", floor_price=101.5))

    closed = book.mark_to_market({coin: mark}, index_prices={coin: mark})

    assert len(closed) == 1
    fill = closed[0]
    assert fill["reason"] == "trailing_stop"
    assert abs(fill["price"] - mark) < 1e-9
