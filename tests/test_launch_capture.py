"""Tests for launch-point capture: microstructure accumulators + flow-confirmed
breakout (shadow feature)."""

from __future__ import annotations

import pytest

from hermes_trader.agents import microstructure as ms_mod
from hermes_trader.models.types import Candle


def _candle(t: int, o, h, l, c, v=100.0) -> Candle:
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_candles(n: int, price: float = 100.0, spread: float = 0.2,
                  vol: float = 100.0, start_t: int = 0) -> list[Candle]:
    out = []
    for i in range(n):
        t = (start_t + i) * 300_000
        out.append(_candle(t, price, price + spread, price - spread, price, vol))
    return out


@pytest.fixture
def fresh_ms(monkeypatch):
    """A clean microstructure singleton per test (no cross-test bleed)."""
    m = ms_mod.Microstructure()
    monkeypatch.setattr(ms_mod, "_instance", m)
    return m


# ── CVD / aggression ────────────────────────────────────────────────────────

def test_aggression_all_buyer(fresh_ms):
    now = 1000.0
    for i in range(10):
        fresh_ms.add_trade(coin="UNI", ts=now - 5 + i, size=10.0,
                           buyer_aggressor=True)
    a = fresh_ms.aggression("UNI", now=now)
    assert a == pytest.approx(1.0)


def test_aggression_mixed_net(fresh_ms):
    now = 1000.0
    fresh_ms.add_trade(coin="X", ts=now - 2, size=30.0, buyer_aggressor=True)
    fresh_ms.add_trade(coin="X", ts=now - 1, size=10.0, buyer_aggressor=False)
    a = fresh_ms.aggression("X", now=now)
    assert a == pytest.approx(0.5)


def test_aggression_none_when_no_flow(fresh_ms):
    assert fresh_ms.aggression("NOPE") is None


def test_old_prints_excluded_from_burst(fresh_ms):
    # Prints well outside the burst window don't feed aggression.
    fresh_ms.add_trade(coin="X", ts=100.0, size=10.0, buyer_aggressor=True)
    assert fresh_ms.aggression("X", now=1000.0) is None


# ── book imbalance ──────────────────────────────────────────────────────────

def test_imbalance_from_sides():
    bids = [{"px": 99, "sz": 30}, {"px": 98, "sz": 10}]
    asks = [{"px": 101, "sz": 10}]
    # (40 - 10) / 50 = 0.6
    assert ms_mod.imbalance_from_sides(bids, asks) == pytest.approx(0.6)


def test_imbalance_empty_is_zero():
    assert ms_mod.imbalance_from_sides([], []) == 0.0


def test_book_imbalance_staleness(fresh_ms):
    fresh_ms.set_book_imbalance(coin="UNI", ts=100.0, imbalance=0.5)
    assert fresh_ms.book_imbalance("UNI", now=100.0) == pytest.approx(0.5)
    assert fresh_ms.book_imbalance("UNI", now=200.0, max_age_sec=10) is None


# ── compression percentile ──────────────────────────────────────────────────

def test_compression_returns_percentile():
    candles = _flat_candles(80, spread=0.2)
    pct = ms_mod.compression_extreme(candles)
    assert pct is not None
    assert 0.0 <= pct <= 100.0


def test_compression_insufficient_data():
    assert ms_mod.compression_extreme(_flat_candles(10)) is None


# ── key level ───────────────────────────────────────────────────────────────

def test_near_key_level_long_on_support():
    # A deep swing low early, then price recovers to near it.
    candles = []
    for i in range(60):
        candles.append(_candle(i * 300_000, 100, 101, 99, 100))
    # insert one deep low
    candles[5] = _candle(5 * 300_000, 100, 100, 95, 96)
    # latest close near (above) the 95 low but within 1 ATR
    candles[-1] = _candle(59 * 300_000, 96, 97, 95.5, 96)
    got = ms_mod.near_key_level(candles, "long", tolerance_atr=5.0)
    assert got is True


# ── flow-confirmed breakout ─────────────────────────────────────────────────

def test_breakout_flow_confirmed_fires_on_single_close():
    from hermes_trader.indicators import triggers

    # 52 quiet bars then a single strong up-close on the launch bar. With the
    # default 2-bar confirm it would NOT fire, but strong buyer flow accepts
    # the launch bar.
    candles = _flat_candles(52, price=100.0, spread=0.2, vol=100.0)
    # launch bar: closes above prior highs, big volume
    candles.append(_candle(52 * 300_000, 100.2, 104, 100.1, 103.5, v=900))
    r = triggers.breakout(candles, lookback=48, min_rvol=1.5,
                          confirm_bars=2, flow_confirm=0.9,
                          flow_confirm_min=0.7)
    assert r["fired"] is True
    assert "flow-confirmed" in r["reason"]


def test_breakout_weak_flow_keeps_confirmation():
    from hermes_trader.indicators import triggers

    candles = _flat_candles(52, price=100.0, spread=0.2, vol=100.0)
    candles.append(_candle(52 * 300_000, 100.2, 104, 100.1, 103.5, v=900))
    r = triggers.breakout(candles, lookback=48, min_rvol=1.5,
                          confirm_bars=2, flow_confirm=0.3,
                          flow_confirm_min=0.7)
    # Only one up-close; weak flow doesn't substitute, so not fired.
    assert r["fired"] is False


def test_breakout_no_flow_default_unchanged():
    from hermes_trader.indicators import triggers

    candles = _flat_candles(53, price=100.0, spread=0.2, vol=100.0)
    r = triggers.breakout(candles, lookback=48, min_rvol=1.5, confirm_bars=2)
    assert r["fired"] is False


# ── time-aligned CVD divergence ─────────────────────────────────────────────

def _wave_prices() -> list[tuple[float, float]]:
    out = []
    for i in range(20):
        if i < 5:
            price = 100.0 + i
        elif i < 10:
            price = 104.0 - (i - 4)
        elif i < 15:
            price = 99.0 + (i - 9) * 1.2
        else:
            price = 105.0 - (i - 14)
        out.append((float(i * 300_000), price))
    return out


def test_cvd_bearish_divergence_requires_matching_time():
    prices = _wave_prices()
    strong_cvd = [(ts, price - 100.0) for ts, price in prices]
    weak_diverging_cvd = [
        (ts, 10.0 if i < 5 else 5.0 if i < 10 else 8.0 if i < 15 else 3.0)
        for i, (ts, _) in enumerate(prices)
    ]

    strong = ms_mod.detect_cvd_divergence(
        prices, strong_cvd, left=3, right=2, min_strength_pct=1.0,
    )
    weak = ms_mod.detect_cvd_divergence(
        prices, weak_diverging_cvd, left=3, right=2, min_strength_pct=10.0,
    )

    assert strong.bearish is False
    assert weak.bearish is True
    assert weak.strength_pct >= 10.0


def test_cvd_divergence_filters_micro_strength():
    prices = _wave_prices()
    cvd = [
        (ts, 10.0 if i < 5 else 9.95 if i < 10 else 10.0 if i < 15 else 9.9)
        for i, (ts, _) in enumerate(prices)
    ]
    got = ms_mod.detect_cvd_divergence(
        prices, cvd, left=3, right=2, min_strength_pct=10.0,
    )
    assert got.bearish is False


def test_trades_capture_appends_jsonl(tmp_path, monkeypatch):
    from hermes_trader.client import ws_client

    captured = []

    class FakeInfo:
        def subscribe(self, sub, cb):
            captured.append((sub, cb))
            return 1

    ws = object.__new__(ws_client.HyperliquidWebSocket)
    ws._info = FakeInfo()
    ws._trades_coins = {"BTC"}
    ws._trades_capture_enabled = False
    monkeypatch.setattr(ws_client, "_TRADES_RAW_DIR", str(tmp_path))

    ws.enable_trades_capture(True)
    assert ws.subscribe_trades("BTC") is True

    trade = {"coin": "BTC", "side": "B", "px": "100", "sz": "1.5",
             "time": 0}
    ws._on_trades({"data": [trade]})

    path = tmp_path / "date=1970-01-01" / "BTC.jsonl"
    assert path.exists()
    assert '"coin":"BTC"' in path.read_text(encoding="utf-8")
