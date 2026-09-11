"""Behavioral contract tests for the bracket/order PRICE and SIZE primitives.

T1 (architecture review 2026-09-11), bracket/cloid batch: the trigger/IOC price
and size path had no direct tests for its two pure arithmetic cores — every SL,
TP, modify and IOC price funnels through them, so a sign or rounding regression
either submits a stop on the wrong side of entry or an order HL asynchronously
rejects (leaving a position unprotected). These tests pin:

  - _signed_price: the single long/short direction coefficient for SL (negative
    distance) vs TP (positive), four quadrants incl. zero;
  - _round_price_for_hl: Hyperliquid tick grid (tick = 10^-(6-sz_dec) perp),
    the 5-significant-figure cap, the IOC aggressiveness direction
    (BUY->ceiling, SELL->floor, None->half-up), and non-positive -> "0";
  - _min_order_size: ceil-up to the size tick so an integer-size coin can never
    be rounded below the HL $ minimum;
  - the implicit fund-safety invariant: with a positive width/ceiling, a backup
    SL price produced from _signed_price always lands on the protective side of
    entry (long below / short above), so it can never flip into a take-profit.

All functions are pure (Decimal/number in, str/number out); _min_order_size's
only non-pure seam is the config floor resolver, stubbed to a constant. Tests
LOCK EXISTING BEHAVIOR and change no trading logic.
"""

import pytest

from hermes_trader.agents import executor
from hermes_trader.client import exchange

# ── _signed_price: long/short direction arithmetic ──────────────────────────

def test_signed_price_long_sl_below_entry():
    # is_buy=True denotes a LONG position; SL caller passes negative distance.
    assert executor._signed_price(100.0, -2.0, True) == 98.0


def test_signed_price_long_tp_above_entry():
    assert executor._signed_price(100.0, 2.0, True) == 102.0


def test_signed_price_short_sl_above_entry():
    # SHORT position (is_buy=False); protective stop is ABOVE entry, still a
    # negative distance from the caller.
    assert executor._signed_price(100.0, -2.0, False) == 102.0


def test_signed_price_short_tp_below_entry():
    assert executor._signed_price(100.0, 2.0, False) == 98.0


def test_signed_price_zero_distance_is_identity():
    assert executor._signed_price(100.0, 0.0, True) == 100.0
    assert executor._signed_price(100.0, 0.0, False) == 100.0


# ── SL never crosses entry (implicit fund-safety invariant) ─────────────────

@pytest.mark.parametrize("is_buy", [True, False])
def test_backup_sl_stays_on_protective_side_for_any_positive_width(is_buy):
    """Mirror the executor backup-SL construction
    (executor.py _place_backup_sl): sl_px = _signed_price(entry, -entry*w/100).
    For every positive width the SL must sit strictly on the loss side:
    long below entry, short above entry. A flipped sign here turns the stop
    into a take-profit and silently removes downside protection."""
    entry = 123.456
    for width_pct in (0.01, 0.4, 1.5, 3.0, 15.0):
        sl_px = executor._signed_price(entry, -entry * width_pct / 100.0, is_buy)
        if is_buy:
            assert sl_px < entry
        else:
            assert sl_px > entry


# ── _round_price_for_hl: non-positive guard ─────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, -1.0, -0.0001])
def test_round_non_positive_returns_zero(bad):
    assert exchange._round_price_for_hl(bad, 2) == "0"


# ── tick grid vs 5-sigfig: the STRICTER of the two binds ────────────────────
# px_decimals = min(tick_decimals=6-sz_dec, sigfig_decimals=5-int_digits).
# For normal ~3-digit prices the 5-sigfig cap binds (2 decimals); the coarser
# tick only shows through for high sz_decimals. Assert the actual min() rule.

def test_round_sigfig_cap_binds_for_three_digit_price():
    # sz_dec=2 allows tick 1e-4 (4 dp), but a 3-digit int part allows only
    # 5-3=2 dp → sigfig is stricter → 101.12.
    assert exchange._round_price_for_hl(101.123456, 2) == "101.12"


def test_round_coarse_tick_binds_for_high_sz_dec():
    # sz_dec=5 → tick 1e-1 (1 dp), stricter than sigfig → 100.1.
    assert exchange._round_price_for_hl(100.123456, 5) == "100.1"
    # sz_dec=6 → tick 1.0 → whole number.
    assert exchange._round_price_for_hl(100.123456, 6) == "100"


def test_round_result_is_on_a_valid_grid():
    """The output is never finer than EITHER the tick or the 5-sigfig cap."""
    from decimal import Decimal
    for price, sd in [(101.123456, 2), (100.123456, 5), (12.345678, 3)]:
        out = float(exchange._round_price_for_hl(price, sd))
        tick = 10.0 ** -(max(0, 6 - sd))
        # on tick grid
        assert abs(out / tick - round(out / tick)) < 1e-9
        # at most 5 significant figures
        assert len(Decimal(str(out)).normalize().as_tuple().digits) <= 5


# ── 5 significant figures cap ───────────────────────────────────────────────

def test_round_five_sigfig_on_large_price():
    # 5-digit integer part -> 0 decimals; rounded half-up to whole.
    assert exchange._round_price_for_hl(12345.67, 3) == "12346"
    # 6-digit integer part -> whole number, no decimal point.
    assert exchange._round_price_for_hl(123456.7, 0) == "123457"


def test_round_five_sigfig_four_digit_int():
    # 4-digit int part -> 1 decimal, capped at 5 sig figs.
    assert exchange._round_price_for_hl(1234.567, 3) == "1234.6"


def test_round_small_price_tick_can_override_sigfig():
    # 0.22882 with sz_dec=5: sigfig allows 5 dp but the tick is 1e-1, so the
    # coarse tick binds → 0.2 (HL derives a wide tick for high-sz_dec coins).
    assert exchange._round_price_for_hl(0.22882, 5) == "0.2"


def test_round_low_sz_dec_small_price_uses_sigfig():
    # sz_dec=2 → tick 1e-4 (4 dp), which is stricter than sigfig here, so
    # 0.22882 truncates/rounds to the tick → 0.2288.
    assert exchange._round_price_for_hl(0.22882, 2) == "0.2288"


# ── IOC aggressiveness direction ────────────────────────────────────────────
# At a 3-digit price the 5-sigfig cap gives 2 decimals, so pick a price sitting
# strictly between two 0.01 ticks: 100.005. ceiling->100.01, floor->100.00.

def test_round_buy_rounds_up_to_stay_crossable():
    assert exchange._round_price_for_hl(100.005, 2, is_buy=True) == "100.01"


def test_round_sell_rounds_down_to_stay_crossable():
    assert exchange._round_price_for_hl(100.005, 2, is_buy=False) == "100.00"


def test_round_none_uses_half_up():
    # 100.005 half-up (banker's not used; ROUND_HALF_UP) -> 100.01.
    assert exchange._round_price_for_hl(100.005, 2, is_buy=None) == "100.01"


def test_round_output_is_string():
    # 2-digit int part -> 5-2=3 dp under sigfig (tick looser) → 98.500.
    out = exchange._round_price_for_hl(98.5, 2)
    assert isinstance(out, str)
    assert out == "98.500"


# ── _min_order_size: ceil-up to the size tick ───────────────────────────────

@pytest.fixture
def fixed_min_usd(monkeypatch):
    # Stub the only config seam so the test is independent of env/config.
    monkeypatch.setattr(exchange, "_resolve_min_order_usd", lambda: 10.5)


def test_min_order_size_ceils_to_size_tick_integer_coin(fixed_min_usd):
    # MEGA @ $0.084: 10.5/0.084 = 125 coins exactly at integer precision;
    # a value just under a whole coin must round UP, never down.
    assert exchange._min_order_size(0.084, 0) == 125.0
    # 10.5 / 1.0 = 10.5 -> ceil to 11 whole coins.
    assert exchange._min_order_size(1.0, 0) == 11.0


def test_min_order_size_two_dp_coin(fixed_min_usd):
    # @ $100, sz_dec=2 (tick 0.01): 10.5/100 = 0.105 -> ceil to 0.11.
    assert exchange._min_order_size(100.0, 2) == 0.11
    # ...worth at least the floor.
    assert 0.11 * 100.0 >= 10.5


def test_min_order_size_never_below_floor(fixed_min_usd):
    for price, sd in [(100.0, 2), (0.084, 0), (1.0, 0), (50000.0, 2),
                      (3.33, 3)]:
        size = exchange._min_order_size(price, sd)
        assert size * price + 1e-9 >= 10.5
