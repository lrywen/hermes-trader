"""Volume Profile 计算逻辑的确定性模拟数据测试。"""

from __future__ import annotations

import pytest

from hermes_trader.indicators.volume_profile import volume_profile
from hermes_trader.models.types import Candle


def _candle(i: int, low: float, high: float, volume: float,
            close: float | None = None) -> Candle:
    return Candle(
        t=i * 300_000,
        o=low,
        h=high,
        l=low,
        c=close if close is not None else (low + high) / 2,
        v=volume,
    )


def test_volume_profile_detects_high_volume_poc():
    candles = [
        _candle(0, 90.0, 92.0, 100.0),
        _candle(1, 94.0, 96.0, 100.0),
        _candle(2, 99.0, 101.0, 700.0),
        _candle(3, 104.0, 106.0, 100.0),
    ]

    vp = volume_profile(candles, bins=10, value_area_pct=70.0)

    assert vp is not None
    assert vp.bin_size == pytest.approx(1.6)
    assert vp.poc == pytest.approx(100.4)
    assert vp.val <= vp.poc <= vp.vah


def test_volume_profile_distributes_volume_by_price_overlap():
    candles = [_candle(0, 90.0, 110.0, 100.0)]
    vp = volume_profile(candles, bins=10, value_area_pct=70.0)

    assert vp is not None
    assert sum(volume for _, volume in vp.bins) == pytest.approx(100.0)
    assert vp.poc == pytest.approx(91.0)
    assert vp.vah >= vp.poc
    assert vp.val <= vp.poc


def test_volume_profile_position_and_distance_helpers():
    candles = [
        _candle(0, 98.0, 99.0, 100.0),
        _candle(1, 99.0, 100.0, 500.0),
        _candle(2, 100.0, 101.0, 100.0),
    ]
    vp = volume_profile(candles, bins=3, value_area_pct=70.0)

    assert vp is not None
    if vp.vah > vp.val:
        assert vp.position_pct(vp.val) == pytest.approx(0.0)
        assert vp.position_pct(vp.vah) == pytest.approx(100.0)
        assert vp.position_pct((vp.val + vp.vah) / 2) == pytest.approx(50.0)
    else:
        assert vp.position_pct(vp.val) is None
    assert vp.distance_from_poc_pct(vp.poc * 1.01) == pytest.approx(1.0)


def test_volume_profile_invalid_input_returns_none():
    assert volume_profile([], bins=10) is None
    assert volume_profile([_candle(0, 100.0, 100.0, 100.0)], bins=10) is None


def test_volume_profile_atr_bins_and_chase_risk():
    candles = [
        _candle(i, 98.0 + i * 0.1, 100.0 + i * 0.1, 100.0)
        for i in range(30)
    ]
    vp = volume_profile(candles, bins=50, atr_bins=True, atr_period=14,
                        atr_multiple=0.25)

    assert vp is not None
    assert vp.bin_size > 0
    assert vp.chase_risk(vp.vah, atr_value=1.0) >= 0.5
    assert vp.distance_from_poc_atr(vp.poc + 2.0, atr_value=1.0) == pytest.approx(2.0)
