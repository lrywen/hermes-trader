"""Volume Profile：按价格区间分配K线成交量。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from hermes_trader.indicators.math import atr, candle_val

# B11: hard ceiling on ATR-derived bin count. The default atr_multiple=0.25
# yields a handful of bins on real data; anything resolving past this cap is a
# misconfiguration (pathologically small atr_multiple) that would otherwise
# allocate an unbounded list before the OOM killer intervenes.
_MAX_ATR_BINS = 10_000


@dataclass(frozen=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    bin_size: float
    bins: list[tuple[float, float]]
    atr_last: float | None = None

    def position_pct(self, price: float) -> float | None:
        """VAL处为0，VAH处为100；范围为空时返回None。"""
        rng = self.vah - self.val
        if rng <= 0:
            return None
        return max(0.0, min(100.0, (price - self.val) / rng * 100.0))

    def distance_from_poc_pct(self, price: float) -> float | None:
        if self.poc <= 0:
            return None
        return (price - self.poc) / self.poc * 100.0

    def distance_from_poc_atr(self, price: float, atr_value: float | None
                               ) -> float | None:
        # Signed: >0 above POC, <0 below POC. The old abs() made "chasing up"
        # indistinguishable from "dipping below POC" (full review 2026-10-25
        # V1: 52.6% of risk==1.0 samples were actually below POC). Observational
        # callers that need a magnitude take abs() themselves.
        if atr_value is None or atr_value <= 0:
            return None
        return (price - self.poc) / atr_value

    def chase_risk(self, price: float, atr_value: float | None = None
                   ) -> float:
        """Signed chase risk in [0, 1]; observation only, never triggers trades.

        V1 (full review 2026-10-25): only prices ABOVE the POC are a chase, so
        anything at/below POC returns 0.0 outright.

        V2: normalize the POC extension by the value-area half-width
        ``(vah-val)/2`` and map it continuously — extension of one VA half-width
        above POC maps to 1.0. The old code combined a discontinuous position_pct
        ladder with a hard ATR-distance clamp that saturated at risk=1.0 for
        ~64% of samples, so it carried almost no information. ATR stays the
        fallback scale when the value area is degenerate.
        """
        if price <= self.poc:
            return 0.0

        rng = self.vah - self.val
        half_width = rng / 2.0 if rng > 0 else 0.0
        if half_width > 0:
            extension = (price - self.poc) / half_width
        elif atr_value is not None and atr_value > 0:
            # Degenerate VA: fall back to one ATR == full risk.
            extension = (price - self.poc) / atr_value
        else:
            return 0.0
        return round(min(1.0, max(0.0, extension)), 4)


def _candle_value(c: Any, key: str) -> float:
    return candle_val(c, key)


def _resolve_bin_size(candles: list[Any], bins: int, lo: float, hi: float,
                      atr_bins: bool, atr_period: int,
                      atr_multiple: float) -> tuple[float, int, float | None]:
    fixed_size = (hi - lo) / int(bins)
    atr_values = atr(candles, atr_period)
    finite_values = [v for v in atr_values if math.isfinite(v) and v > 0.0]
    atr_last = finite_values[-1] if finite_values else None
    if not atr_bins:
        return fixed_size, int(bins), atr_last

    if not finite_values:
        return fixed_size, int(bins), atr_last
    dynamic_size = finite_values[-1] * float(atr_multiple)
    dynamic_bins = max(1, int(math.ceil((hi - lo) / dynamic_size)))
    return max(dynamic_size, (hi - lo) / 10_000.0), dynamic_bins, atr_last


def volume_profile(
    candles: list[Any],
    *,
    bins: int = 50,
    value_area_pct: float = 70.0,
    atr_bins: bool = False,
    atr_period: int = 14,
    atr_multiple: float = 0.25,
) -> VolumeProfile | None:
    """从OHLCV K线计算 POC、VAH、VAL。

    每根K线成交量按其与价格bin的重叠比例分配，而不是全部分配给收盘价。
    """
    if len(candles) < 1 or bins <= 0:
        return None
    # B11: atr_bins derives bin count from ATR*atr_multiple; a non-positive
    # multiple makes dynamic_size <= 0 -> a gigantic bin allocation (e.g.
    # atr_multiple=1e-9 -> ~1.8e9 bins / 11GB+ before OOM-kill). Positive but
    # tiny multiples also explode; cap at a hard bin ceiling and return None
    # like any other unusable input.
    if atr_bins and (atr_multiple <= 0.0 or atr_period < 1):
        return None
    if not 0.0 < value_area_pct <= 100.0:
        return None

    lows = [_candle_value(c, "l") for c in candles]
    highs = [_candle_value(c, "h") for c in candles]
    lo = min(lows)
    hi = max(highs)
    if hi <= lo:
        return None

    bin_size, resolved_bins, atr_last = _resolve_bin_size(
        candles, int(bins), lo, hi, atr_bins, atr_period, atr_multiple,
    )
    if resolved_bins <= 0 or resolved_bins > _MAX_ATR_BINS:
        return None
    counts = [0.0] * resolved_bins
    edges = [lo + i * bin_size for i in range(resolved_bins + 1)]

    for candle in candles:
        c_lo = _candle_value(candle, "l")
        c_hi = _candle_value(candle, "h")
        volume = _candle_value(candle, "v")
        c_rng = c_hi - c_lo
        if c_hi <= c_lo or volume <= 0:
            continue
        start = min(int((c_lo - lo) / bin_size), resolved_bins - 1)
        end = min(int((c_hi - lo) / bin_size), resolved_bins - 1)
        for i in range(start, end + 1):
            overlap = min(c_hi, edges[i + 1]) - max(c_lo, edges[i])
            if overlap > 0:
                counts[i] += volume * overlap / c_rng

    total = sum(counts)
    if total <= 0:
        return None

    poc_i = max(range(resolved_bins), key=lambda i: counts[i])
    target = total * (value_area_pct / 100.0)
    included = {poc_i}
    volume_inside = counts[poc_i]
    above = poc_i + 1
    below = poc_i - 1

    while volume_inside < target and (above < resolved_bins or below >= 0):
        above_vol = counts[above] if above < resolved_bins else -1.0
        below_vol = counts[below] if below >= 0 else -1.0
        if above_vol >= below_vol:
            included.add(above)
            volume_inside += above_vol
            above += 1
        else:
            included.add(below)
            volume_inside += below_vol
            below -= 1

    val_i = min(included)
    vah_i = max(included)
    center = lambda i: edges[i] + bin_size / 2.0
    return VolumeProfile(
        poc=center(poc_i),
        vah=center(vah_i),
        val=center(val_i),
        bin_size=bin_size,
        bins=[(center(i), counts[i]) for i in range(resolved_bins)],
        atr_last=atr_last,
    )
