"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _mk_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _breakout_candles(prior_n=48, base=100.0, vol=1000.0):
    """`prior_n` ranging bars capped at `base`, then the caller appends breaks."""
    cs = []
    for i in range(prior_n):
        # range [base-1, base], high = base
        cs.append(_mk_candle(i, base - 0.5, base, base - 1, base - 0.2, vol))
    return cs


def _candle_1h(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_candles(n, price=100.0, vol=1000.0, rng=0.5):
    """Choppy/flat candles oscillating around `price` (produces low ADX)."""
    out = []
    for i in range(n):
        s = 1.0 if (i % 2 == 0) else -1.0
        c = price + s * (i % 3) * 0.1
        out.append(_mk_candle(i, c, c + rng, c - rng, c, vol))
    return out


def _healthy_trend_candles(n=100, start=100.0, vol=1000.0):
    """Rising trend with regular pullbacks so RSI lands in a healthy 50-70
    band. Pattern: 2 up bars then 1 down bar with a deeper retracement."""
    out = []
    price = start
    for i in range(n):
        if i % 3 == 2:
            price -= 0.5  # deeper pullback
        else:
            price += 0.4  # impulse
        out.append(_mk_candle(i, price, price + 0.3, price - 0.1, price, vol + i))
    return out


def _trend_candles(n, start=100.0, step=0.5, vol=1000.0):
    """Steadily rising candles (produces high ADX)."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(_mk_candle(i, c, c + 0.8, c - 0.2, c, vol + i))
    return out


def test_candle_model_and_getitem():
    c = Candle(t=1, o=2.0, h=3.0, l=1.0, c=2.5, v=100.0)
    assert c.c == 2.5
    assert c["c"] == 2.5 and c["t"] == 1


def test_candle_val_dict_and_obj():
    from hermes_trader.indicators.math import candle_val
    assert candle_val(Candle(t=1, o=1, h=2, l=0.5, c=1.5, v=9), "c") == 1.5
    assert candle_val({"c": 7.0}, "c") == 7.0
    assert candle_val({}, "c") == 0


def test_ema_sma():
    from hermes_trader.indicators.math import ema, sma
    vals = [float(i) for i in range(50)]
    assert len(ema(vals, 8)) == 50
    assert len(sma(vals, 8)) == 50
    assert ema([], 8) == []


def test_directional_momentum_triggers_are_symmetric():
    """uptrend_momentum fires on a sustained UP move, downtrend_momentum on a
    sustained DOWN move, and each stays silent on the opposite/flat — the
    symmetric surfacing pair that lets the bot short downtrends."""
    from hermes_trader.indicators.triggers import downtrend_momentum, uptrend_momentum
    from hermes_trader.models.types import Candle
    up = [Candle(t=i, o=100, h=101, l=99, c=100.0 * (1.0006 ** i), v=10) for i in range(80)]   # ~+5% over 80
    down = [Candle(t=i, o=100, h=101, l=99, c=100.0 * (0.9994 ** i), v=10) for i in range(80)] # ~-5% over 80
    flat = [Candle(t=i, o=100, h=101, l=99, c=100.0, v=10) for i in range(80)]
    assert uptrend_momentum(up, 72, 3.0)["fired"] is True
    assert downtrend_momentum(up, 72, 3.0)["fired"] is False
    assert downtrend_momentum(down, 72, 3.0)["fired"] is True
    assert uptrend_momentum(down, 72, 3.0)["fired"] is False
    assert uptrend_momentum(flat, 72, 3.0)["fired"] is False
    assert downtrend_momentum(flat, 72, 3.0)["fired"] is False
    # downtrend weight stays 0 (negative EV chase); uptrend was promoted to a
    # scored weight 0.20 (only stable positive-EV trigger, 2026-09-28 reweight).
    from hermes_trader.agents.config import get_config
    w = get_config()["weights"]
    assert w["uptrendMomentum"] == 0.20 and w["downtrendMomentum"] == 0.0


def test_atr_rsi_adx_produce_finite_output():
    from hermes_trader.indicators.math import adx, atr, rsi
    cs = _candles(150)
    for fn in (atr, rsi, adx):
        out = fn(cs, 14)
        assert len(out) == 150
        assert any(math.isfinite(x) for x in out)


def test_rsi_and_adx_stay_in_0_100_bound():
    """RSI and ADX are mathematically bounded 0-100 — every finite output
    value must respect that. A negative RSI means the loss/gain accumulator
    math is broken (regression guard for the avg_l sign bug)."""
    from hermes_trader.indicators.math import adx, rsi
    # exercise rising, falling and choppy series so the smoothing loop runs
    rising = [Candle(t=i, o=100 + i, h=101 + i, l=99 + i, c=100 + i, v=10) for i in range(150)]
    falling = [Candle(t=i, o=250 - i, h=251 - i, l=249 - i, c=250 - i, v=10) for i in range(150)]
    choppy = _candles(150)
    for series in (rising, falling, choppy):
        for fn in (rsi, adx):
            for v in fn(series, 14):
                if math.isfinite(v):
                    assert 0.0 <= v <= 100.0, f"{fn.__name__} out of bound: {v}"


def test_triggers_return_shape():
    from hermes_trader.indicators.triggers import (
        breakout,
        pct_move_spike,
        range_compression,
        trend_strength,
        volume_spike,
    )
    cs = _candles(150)
    # Audit 2026-09-12: pct_move_spike/volume_spike additionally carry a
    # structured `z` (and pct_move_spike a `direction`) for the σ-burst gate;
    # every trigger still has the four core keys. Assert the common core plus
    # the spike-specific extras rather than one shared exact shape.
    core = {"name", "score", "reason", "fired"}
    for fn in (breakout, range_compression, trend_strength):
        h = fn(cs)
        assert set(h) == core
        assert isinstance(h["fired"], bool)
    for fn in (pct_move_spike, volume_spike):
        h = fn(cs)
        assert core <= set(h)
        assert isinstance(h["fired"], bool)
        assert isinstance(h["z"], float)
    assert "direction" in pct_move_spike(cs)


def test_composite_score_in_range():
    from hermes_trader.indicators.triggers import composite_score, pct_move_spike, volume_spike
    cs = _candles(150)
    weights = {"pctMoveSpike": 0.35, "volumeSpike": 0.25}
    s = composite_score([pct_move_spike(cs), volume_spike(cs)], weights)
    assert 0 <= s <= 100
    assert composite_score([], weights) == 0


def test_momentum_burst_fires_on_large_move():
    from hermes_trader.indicators.triggers import momentum_burst
    flat = [Candle(t=i, o=100, h=100, l=100, c=100.0, v=10) for i in range(10)]
    h = momentum_burst(flat, lookback=2, pct_threshold=4.0)
    assert h["name"] == "momentumBurst" and h["fired"] is False

    # +6% over the last 2 bars — well past a 4% threshold
    surge = flat[:-2] + [
        Candle(t=8, o=103, h=103, l=103, c=103.0, v=10),
        Candle(t=9, o=106, h=106, l=106, c=106.0, v=10),
    ]
    h = momentum_burst(surge, lookback=2, pct_threshold=4.0)
    assert h["fired"] is True
    assert h["score"] > 0
    assert "up" in h["reason"]

    # a downward burst fires too
    crash = flat[:-2] + [
        Candle(t=8, o=97, h=97, l=97, c=97.0, v=10),
        Candle(t=9, o=94, h=94, l=94, c=94.0, v=10),
    ]
    assert momentum_burst(crash, lookback=2, pct_threshold=4.0)["fired"] is True


def test_trend_from_closes_up_down_neutral():
    """EMA20>EMA30 + positive fast-slope → up; opposite → down; flat → neutral."""
    from hermes_trader.agents.market_regime import trend_from_closes
    # Pure uptrend: prices rising linearly
    assert trend_from_closes([100 + i for i in range(60)]) == "up"
    # Pure downtrend
    assert trend_from_closes([200 - i for i in range(60)]) == "down"
    # Pure flat
    assert trend_from_closes([100.0] * 60) == "neutral"
    # Too few candles
    assert trend_from_closes([100.0] * 20) == "neutral"


def test_volume_buildup_1h_fires_on_4h_surge():
    """volumeBuildup1h should fire when the last 4h's avg notional volume
    is >= ratio_threshold × the prior 20h baseline."""
    from hermes_trader.indicators.triggers import volume_buildup_1h
    # 20h baseline at vol=1000, last 4h at vol=3000 → 3× surge
    base = [_candle_1h(i, 100, 101, 99, 100, 1000) for i in range(20)]
    surge = [_candle_1h(i + 20, 100, 101, 99, 100, 3000) for i in range(4)]
    res = volume_buildup_1h(base + surge, ratio_threshold=2.5)
    assert res["fired"] is True
    assert "3.0×" in res["reason"] or "3.00" in res["reason"]

    # Flat: no surge
    flat = [_candle_1h(i, 100, 101, 99, 100, 1000) for i in range(24)]
    res = volume_buildup_1h(flat, ratio_threshold=2.5)
    assert res["fired"] is False


def test_trend_flip_1h_detects_recent_ema_cross():
    """trendFlip1h fires when EMA8 crosses above EMA21 within lookback bars."""
    from hermes_trader.indicators.triggers import trend_flip_1h
    # 25 bars trending down, then 8 bars trending up — fast EMA crosses slow.
    closes = [100 - i for i in range(25)] + [76 + i * 2 for i in range(8)]
    bars = [_candle_1h(i, c, c + 0.5, c - 0.5, c, 1000) for i, c in enumerate(closes)]
    res = trend_flip_1h(bars, lookback_bars=5)
    assert res["fired"] is True
    assert "cross up" in res["reason"]

    # All downtrend: no flip
    down = [_candle_1h(i, 100 - i, 101 - i, 99 - i, 100 - i, 1000) for i in range(30)]
    res = trend_flip_1h(down, lookback_bars=3)
    assert res["fired"] is False


def test_higher_lows_1h_counts_structure():
    """higherLows1h fires when N+ of last 6 1h candles printed higher lows."""
    from hermes_trader.indicators.triggers import higher_lows_1h
    # 7 candles with strictly rising lows: 6/6 higher lows
    rising = [_candle_1h(i, 100, 101, 99 + i, 100 + i, 1000) for i in range(7)]
    res = higher_lows_1h(rising, required=4)
    assert res["fired"] is True
    assert "6/6" in res["reason"]

    # All lows equal: 0/6 → fails
    flat = [_candle_1h(i, 100, 101, 99, 100, 1000) for i in range(7)]
    res = higher_lows_1h(flat, required=4)
    assert res["fired"] is False


def test_compute_indicators_empty_returns_nulls():
    from hermes_trader.agents.research import _compute_indicators
    out = _compute_indicators([])
    assert out["ema8"] is None and out["last_close"] == 0


def test_compute_indicators_thin_history_partial():
    """<21 candles → indicators None but last_close/last_time populated."""
    from hermes_trader.agents.research import _compute_indicators
    candles = [Candle(t=i, o=10, h=11, l=9, c=10 + i, v=100) for i in range(5)]
    out = _compute_indicators(candles)
    assert out["ema8"] is None
    assert out["last_close"] == 14  # 10 + 4
    assert out["last_time"] == 4


def test_compute_indicators_full_history_computes_emas():
    """≥21 candles → EMA/RSI/ATR/ADX numeric, slope detected on rising series."""
    from hermes_trader.agents.research import _compute_indicators
    candles = [Candle(t=i, o=100 + i, h=101 + i, l=99 + i, c=100 + i, v=1000)
               for i in range(40)]
    out = _compute_indicators(candles)
    assert out["ema8"] is not None and out["ema21"] is not None
    assert out["ema8"] > out["ema21"]  # rising series → fast above slow
    assert out["slope_up"] is True
    assert out["last_close"] == 139


def test_obv_accumulates_on_up_closes():
    from hermes_trader.indicators.math import obv
    # three up closes: OBV should be cumulative positive volume
    cs = [
        _mk_candle(0, 100, 101, 99, 100, 1000),
        _mk_candle(1, 100, 101, 99, 101, 500),
        _mk_candle(2, 101, 102, 100, 102, 300),
    ]
    series = obv(cs)
    assert series[0] == 0.0
    assert series[1] == 500.0     # up bar +500
    assert series[2] == 800.0     # up bar +300


def test_obv_distributes_on_down_closes():
    from hermes_trader.indicators.math import obv
    cs = [
        _mk_candle(0, 100, 101, 99, 100, 1000),
        _mk_candle(1, 100, 100, 98, 99, 400),
        _mk_candle(2, 99, 99, 97, 98, 200),
    ]
    series = obv(cs)
    assert series[1] == -400.0
    assert series[2] == -600.0


def test_extension_atr_sign_and_magnitude():
    from hermes_trader.agents import ta_filter
    # A strong uptrend puts price well above EMA21 -> positive extension
    up = _trend_candles(60, start=100.0, step=1.0)
    ext_up = ta_filter._extension_atr(up)
    assert ext_up is not None and ext_up > 0

    # Downtrend -> negative extension
    down = [_mk_candle(i, 100 - i, 101 - i, 99 - i, 100 - i, 1000) for i in range(60)]
    ext_down = ta_filter._extension_atr(down)
    assert ext_down is not None and ext_down < 0

    # Too short -> None
    assert ta_filter._extension_atr(up[:10]) is None


def test_obv_slope_sign():
    from hermes_trader.agents import ta_filter
    up = _trend_candles(40, start=100.0, step=0.5)
    assert ta_filter._obv_slope(up, lookback=10) > 0
    down = [_mk_candle(i, 100 - i * 0.5, 101 - i * 0.5, 99 - i * 0.5, 100 - i * 0.5, 1000)
            for i in range(40)]
    assert ta_filter._obv_slope(down, lookback=10) < 0


def test_volume_confirm_threshold_is_1_2x():
    """The volume confirmation bar must be >= 1.2x the prior 20-bar average.
    The old 0.8x threshold let below-average bars through."""
    from hermes_trader.agents import ta_filter
    # 20 baseline bars at vol=1000, last bar at 1000 (= 1.0x avg) -> NOT confirmed
    flat = [_mk_candle(i, 100, 101, 99, 100, 1000) for i in range(21)]
    assert ta_filter._check_volume_confirm(flat) is False
    # Last bar at 1500 (= 1.5x avg) -> confirmed
    surge = flat[:-1] + [_mk_candle(20, 100, 101, 99, 100, 1500)]
    assert ta_filter._check_volume_confirm(surge) is True
    # Exactly 1.2x boundary -> confirmed
    edge = flat[:-1] + [_mk_candle(20, 100, 101, 99, 100, 1200)]
    assert ta_filter._check_volume_confirm(edge) is True


def test_ta_filter_rejects_overbought_long(monkeypatch):
    """A long-intending impulse at 4h RSI>75 must be REJECTED before paid AI."""
    from hermes_trader.agents import ta_filter

    # Bullish 4h trend but RSI overbought.
    bull = _trend_candles(100, start=100.0, step=0.6)
    flat = _flat_candles(100)
    monkeypatch.setattr(ta_filter, "fetch_hl_candles",
                        lambda *a, **k: bull if a[1] in ("1h", "4h") else flat)

    perception = {
        "coin": "TEST",
        "composite_score": 80,
        "triggers": [
            {"name": "breakout", "fired": True, "score": 8},
            {"name": "momentumBurst", "fired": True, "score": 9},
        ],
    }
    res = ta_filter.analyze_perception(perception)
    rsi = res.get("rsi4h")
    assert rsi is not None and rsi > 75, f"test setup: RSI should be >75, got {rsi}"
    assert res["signal"] == "REJECTED"
    assert "late long" in res["reason"]


def test_ta_filter_rejects_oversold_short(monkeypatch):
    """A short-intending impulse at 4h RSI<25 must be REJECTED."""
    from hermes_trader.agents import ta_filter

    bear = [_mk_candle(i, 100 - i * 0.6, 101 - i * 0.6, 99 - i * 0.6, 100 - i * 0.6, 1000)
            for i in range(100)]
    flat = _flat_candles(100)
    monkeypatch.setattr(ta_filter, "fetch_hl_candles",
                        lambda *a, **k: bear if a[1] in ("1h", "4h") else flat)

    perception = {
        "coin": "TEST",
        "composite_score": 80,
        "triggers": [
            {"name": "downtrendMomentum", "fired": True, "score": 8},
        ],
    }
    res = ta_filter.analyze_perception(perception)
    rsi = res.get("rsi4h")
    assert rsi is not None and rsi < 25, f"test setup: RSI should be <25, got {rsi}"
    assert res["signal"] == "REJECTED"
    assert "late short" in res["reason"]


def test_ta_filter_passes_healthy_long(monkeypatch):
    """A bullish impulse with RSI in a healthy range must not be vetoed."""
    from hermes_trader.agents import ta_filter

    up = _healthy_trend_candles(100)
    flat = _flat_candles(100)
    monkeypatch.setattr(ta_filter, "fetch_hl_candles",
                        lambda *a, **k: up if a[1] in ("1h", "4h") else flat)

    perception = {
        "coin": "TEST",
        "composite_score": 80,
        "triggers": [{"name": "breakout", "fired": True, "score": 8}],
    }
    res = ta_filter.analyze_perception(perception)
    rsi = res.get("rsi4h")
    assert rsi is not None and 40 < rsi < 75, f"test setup: RSI should be 40-75, got {rsi}"
    # Not the late-long veto. It may be WEAK/CONFIRMED, but reason must not be veto.
    assert "late long" not in (res.get("reason") or "")
    assert res["signal"] != "REJECTED" or "late long" not in (res.get("reason") or "")


def test_candle_cache_lookup_counter_miss_then_hit(monkeypatch):
    """fetch_hl_candles increments hermes_candle_cache_lookups_total per
    outcome: first call = miss (cold), second call within TTL = hit."""
    import hermes_trader.client.hl_client as hl
    from hermes_trader import metrics

    class _FakeCache:
        def __init__(self):
            self.store = {}

        def get(self, k):
            return self.store.get(k)

        def set(self, k, v, ttl=None):
            self.store[k] = v

    fake_cache = _FakeCache()
    monkeypatch.setattr(hl, "_CANDLE_CACHE", fake_cache)
    # No concurrent coalescing in this single-threaded test.
    monkeypatch.setattr(hl, "_inflight", {})
    monkeypatch.setattr(hl, "_inflight_results", {})
    candles = _flat_candles(30)

    def fake_raw(coin, interval, count, cache_key=None, opportunistic=False):
        # The real raw() writes the cache on success (hl_client.py ~L546).
        fake_cache.set(cache_key, candles)
        return candles

    monkeypatch.setattr(hl, "_fetch_hl_candles_raw", fake_raw)

    def val(result):
        return (metrics.CANDLE_CACHE_LOOKUPS
                .labels(interval="4h", result=result)._value.get())

    miss0, hit0 = val("miss"), val("hit")
    hl.fetch_hl_candles("ZZZ", "4h", 30)   # cold → miss
    assert val("miss") == miss0 + 1 and val("hit") == hit0
    hl.fetch_hl_candles("ZZZ", "4h", 30)   # cached → hit
    assert val("miss") == miss0 + 1 and val("hit") == hit0 + 1


def test_breakout_requires_two_bars_to_hold():
    """A single close above the prior high must NOT fire (fakeout risk); two
    consecutive closes above must fire when RVOL confirms."""
    from hermes_trader.indicators.triggers import breakout
    # 50 prior bars + 2 confirm bars = 52 >= lookback(48)+confirm_bars(2)+1 = 51
    prior = _breakout_candles(prior_n=50, base=100.0, vol=1000)

    # First confirm bar closes back INSIDE the range (99.5 below prior high
    # 100), then the last bar breaks above (102). Only 1 of 2 confirm bars is
    # above the high -> "not held 2 bars", fired=False (fakeout risk).
    one_bar = prior + [
        _mk_candle(50, 100, 100.2, 99, 99.5, 1000),
        _mk_candle(51, 99.5, 102, 99.5, 102, 4000),
    ]
    r = breakout(one_bar, lookback=48, min_rvol=1.5, confirm_bars=2)
    assert r["fired"] is False
    assert "not held 2 bars" in r["reason"] or "unconfirmed" in r["reason"]

    # Two consecutive closes above the high on high volume -> fires.
    two_bar = prior + [
        _mk_candle(50, 100, 102, 100, 102, 4000),
        _mk_candle(51, 102, 103, 101, 102.5, 4000),
    ]
    r = breakout(two_bar, lookback=48, min_rvol=1.5, confirm_bars=2)
    assert r["fired"] is True
    assert "held 2 bars" in r["reason"]


def test_breakout_confirm_bars_equals_one_restores_old_behavior():
    """confirm_bars=1 must still fire on a single strong close (back-compat)."""
    from hermes_trader.indicators.triggers import breakout
    # 50 prior + 1 = 51 >= lookback(48)+confirm_bars(1)+1 = 50
    prior = _breakout_candles(prior_n=50, base=100.0, vol=1000)
    one_bar = prior + [_mk_candle(50, 100, 103, 100, 102, 4000)]
    r = breakout(one_bar, lookback=48, min_rvol=1.5, confirm_bars=1)
    assert r["fired"] is True


def test_trend_strength_halves_score_above_adx_45(monkeypatch):
    """ADX>45 is a late/extended trend — its score must be HALF the raw
    last_adx/4 map, so the composite stops surfacing trend-exhaustion names."""
    from hermes_trader.indicators import triggers
    cs = _trend_candles(100)
    # Force a finite, very high ADX reading.
    monkeypatch.setattr(triggers, "adx", lambda candles, period: [float("nan")] * (len(candles) - 1) + [50.0])
    r = triggers.trend_strength(cs)
    assert r["fired"] is True
    assert "late/extended" in r["reason"]
    # raw 50/4 = 12.5 -> capped 10 -> halved = 5.0
    assert abs(r["score"] - 5.0) < 1e-9


def test_trend_strength_full_score_in_mature_trend(monkeypatch):
    from hermes_trader.indicators import triggers
    cs = _trend_candles(100)
    monkeypatch.setattr(triggers, "adx", lambda candles, period: [float("nan")] * (len(candles) - 1) + [30.0])
    r = triggers.trend_strength(cs)
    assert r["fired"] is True
    assert "late/extended" not in r["reason"]
    # raw 30/4 = 7.5, not halved
    assert abs(r["score"] - 7.5) < 1e-9


def test_squeeze_breakout_coupling_boosts_breakout():
    from hermes_trader.agents.perception import _apply_squeeze_breakout_coupling
    hits = [
        {"name": "breakout", "score": 6.0, "reason": "breakout above high", "fired": True},
        {"name": "rangeCompression", "score": 9.0, "reason": "BB squeeze", "fired": True},
    ]
    _apply_squeeze_breakout_coupling(hits)
    bo = next(h for h in hits if h["name"] == "breakout")
    assert bo["score"] == 8.0
    assert "[squeeze-resolved]" in bo["reason"]


def test_squeeze_breakout_coupling_caps_at_10():
    from hermes_trader.agents.perception import _apply_squeeze_breakout_coupling
    hits = [
        {"name": "breakout", "score": 9.5, "reason": "breakout above high", "fired": True},
        {"name": "rangeCompression", "score": 9.0, "reason": "BB squeeze", "fired": True},
    ]
    _apply_squeeze_breakout_coupling(hits)
    bo = next(h for h in hits if h["name"] == "breakout")
    assert bo["score"] == 10.0


def test_squeeze_breakout_coupling_noop_without_squeeze():
    from hermes_trader.agents.perception import _apply_squeeze_breakout_coupling
    hits = [
        {"name": "breakout", "score": 6.0, "reason": "breakout above high", "fired": True},
        {"name": "rangeCompression", "score": 0.0, "reason": "BB normal", "fired": False},
    ]
    _apply_squeeze_breakout_coupling(hits)
    bo = next(h for h in hits if h["name"] == "breakout")
    assert bo["score"] == 6.0
    assert "[squeeze-resolved]" not in bo["reason"]


def test_classify_candles_detects_chop_on_low_adx():
    """EMA-neutral + low ADX → 'chop'. A strong trend must win over chop."""
    from hermes_trader.agents.market_regime import _classify_candles
    flat = _flat_candles(100, price=100.0)
    regime = _classify_candles(flat)
    assert regime == "chop", f"flat/low-ADX tape should be chop, got {regime}"


def test_classify_candles_trend_overrides_chop():
    from hermes_trader.agents.market_regime import _classify_candles
    up = _trend_candles(100, start=100.0, step=0.5)
    assert _classify_candles(up) == "up"
    down = [_mk_candle(i, 200 - i * 0.5, 201 - i * 0.5, 199 - i * 0.5, 200 - i * 0.5, 1000)
            for i in range(100)]
    assert _classify_candles(down) == "down"
