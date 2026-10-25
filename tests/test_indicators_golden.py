"""Golden / regression pins for the indicator layer.

Context (full review 2026-10-25, items I5/V5 + S7/P2-1): the indicator math
was reworked six times with no regression guardrail, and two concrete defects
survived into replay/audit scripts:

* ``candle_val`` returned raw dict values unchanged, so numeric fields in
  raw HL JSON (which arrive as *strings*, e.g. ``"179.55"``) raised
  ``TypeError: unsupported operand type(s) for -: 'str' and 'str'`` the moment
  an indicator subtracted them. This was the only defect that actually crashed
  a replay script.
* ``composite_score`` guarded a NaN/inf aggregate (plain ``min(100, nan)``
  returns 100 — a phantom max score), but the ``composite_score_aged`` twin
  had no such guard.

These tests pin hand-computed values for the core indicators (no numpy/pandas
in the runtime image, so the oracle is arithmetic, not a second library) and
lock the two fail-closed behaviours. They do NOT assert any alpha outcome.
"""
from __future__ import annotations

import math

import pytest

from hermes_trader.indicators import math as imath
from hermes_trader.indicators import triggers
from hermes_trader.models.types import Candle


def _c(o, h, l, c, v=1000.0, t=0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


# ── candle_val coercion (I5/V5 regression) ─────────────────────────────────

def test_candle_val_coercing_numeric_strings_subtracts():
    # Raw HL JSON shape: OHLCV fields are strings.
    d = {"o": "179.55", "c": "179.25", "v": "1000"}
    assert isinstance(imath.candle_val(d, "o"), float)
    assert imath.candle_val(d, "o") - imath.candle_val(d, "c") == pytest.approx(0.30)


def test_candle_val_missing_key_fails_closed_to_nan():
    # A missing OHLC field must NOT default to 0 (that fabricated a -100%
    # spike on the next return); it degrades to NaN so guards reject the bar.
    assert math.isnan(imath.candle_val({"c": 1.0}, "absent"))
    assert math.isnan(imath.candle_val({"o": 1.0}, "c"))


def test_candle_val_none_and_unparseable_fail_closed_to_nan():
    assert math.isnan(imath.candle_val({"c": None}, "c"))
    assert math.isnan(imath.candle_val({"c": "not-a-number"}, "c"))


def test_candle_val_candle_object_stays_float_and_timestamp_intable():
    c = _c(1.0, 2.0, 0.5, 1.5)
    assert imath.candle_val(c, "c") == 1.5
    ts = Candle(t=1760000000000, o=1, h=1, l=1, c=1, v=1)
    assert int(imath.candle_val(ts, "t")) == 1760000000000


def test_string_dict_feeds_indicator_without_typeerror():
    # End-to-end: a string-valued HL dict series must not crash an indicator.
    ds = [{"t": i, "o": "100", "h": "101", "l": "99", "c": str(100 + i),
           "v": "1000"} for i in range(20)]
    closes = [imath.candle_val(x, "c") for x in ds]
    assert closes == [float(100 + i) for i in range(20)]


# ── hand-computed indicator goldens (no third-party oracle) ────────────────

def test_sma_golden():
    out = imath.sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2:] == pytest.approx([2.0, 3.0, 4.0])


def test_ema_golden():
    # alpha = 2/4 = 0.5; seed = first value.
    out = imath.ema([2.0, 4.0, 2.0, 4.0], 3)
    assert out[0] == 2.0
    assert out[1] == pytest.approx(3.0)
    assert out[2] == pytest.approx(2.5)
    assert out[3] == pytest.approx(3.25)


def test_rsi_flat_tape_is_neutral_not_overbought():
    # All-equal closes: no movement is no information -> 50, not 100 (P1-1).
    flat = [_c(10, 10, 10, 10) for _ in range(20)]
    rsi = imath.rsi(flat, 14)
    assert rsi[-1] == 50.0


def test_rsi_monotone_up_is_100():
    candles = [_c(100 + i, 100 + i, 100 + i, 100 + i) for i in range(20)]
    rsi = imath.rsi(candles, 14)
    assert rsi[-1] == 100.0


def test_sma_dirty_window_recovers_once_nan_slides_out():
    vals = [1.0, 2.0, float("nan"), 4.0, 5.0, 6.0]
    out = imath.sma(vals, 3)
    # windows touching index 2 are NaN; [3,4,5] and [4,5,6] recover.
    assert math.isnan(out[2]) and math.isnan(out[3]) and math.isnan(out[4])
    assert out[5] == pytest.approx(5.0)


# ── composite fail-closed (P2-1 + S7 twin) ─────────────────────────────────

_W = {"a": 0.4, "b": 0.3}


def _hit(name, score, fired=True):
    return {"name": name, "score": score, "fired": fired}


def test_composite_score_nan_aggregate_fails_closed():
    assert triggers.composite_score([_hit("a", float("nan"))], _W) == 0.0
    assert triggers.composite_score([_hit("a", float("inf"))], _W) == 0.0


def test_composite_score_aged_nan_aggregate_fails_closed():
    # Same NaN hit through the aged twin must NOT surface the 100 ceiling.
    hits = [_hit("a", float("nan"))]
    aged = triggers.composite_score_aged(
        hits, _W, current_bar_ms=1000, onset_ms={}, halflife_ms={})
    assert aged == 0.0


def test_composite_score_aged_normal_finite_matches_expected():
    # New trigger (no onset -> factor 1.0): single fired hit weight 0.4 over
    # total weight 0.7, score 10 -> (10*0.4/0.7)*10 = 57.142...
    hits = [_hit("a", 10.0), _hit("b", 0.0)]
    aged = triggers.composite_score_aged(
        hits, _W, current_bar_ms=1000, onset_ms={}, halflife_ms={})
    assert aged == pytest.approx((10.0 * 0.4 / 0.7) * 10)
