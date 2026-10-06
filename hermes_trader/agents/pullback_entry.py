"""Pullback-to-support entry detector (P1 entry timing).

The prior pullback attempts failed because they only waited for the RSI to cool
while price was still mid-range — then bought right before the next leg DOWN
(MFE 0 for VVV). "Cooling" is not a setup. A real pullback entry requires three
independent things, which this module detects with PURE functions over candles:

1. ESTABLISHED UP TREND — the higher-timeframe structure is up (price above a
   rising long EMA / a positive directional move over the lookback). We only
   buy dips within a real uptrend, never catch falling knives in a downtrend.
2. PULLBACK TO A SUPPORT ZONE — price has corrected (not chased) and is now AT a
   defined support: a rising moving average, a prior swing high acting as new
   floor (breakout retest), within an ATR-scaled tolerance. Entry is near
   support so the stop is tight and risk/reward good — not mid-air.
3. STOP-DECLINE CONFIRMATION — at support the down-leg LOSES STEAM (shrinking
   range / volume, higher low) and a fresh up-bar prints (close back above the
   prior bar / short EMA). We enter ON the first confirmed turn, not while bars
   are still making lower lows. This is the piece the earlier attempts lacked.

All functions are defensive and return None/False on bad data. No network, no
config IO. Callers supply ordered candles (oldest→newest) and tolerance params.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from hermes_trader.indicators.math import ema


def _v(c, key: str) -> float:
    return float(c.get(key) if isinstance(c, dict) else getattr(c, key))


# ── 1. Established uptrend ─────────────────────────────────────────────────

def established_uptrend(
    candles: Sequence,
    *,
    trend_ema_p: int = 50,
    lookback: int = 50,
) -> bool:
    """Higher-timeframe up: latest close above a RISING long EMA and the price
    higher than ``lookback`` bars ago. Both must agree."""
    if len(candles) < max(trend_ema_p, lookback) + 1:
        return False
    closes = [_v(c, "c") for c in candles]
    e = ema(closes, trend_ema_p)
    if not (math.isfinite(e[-1]) and math.isfinite(e[-3])):
        return False
    ema_rising = e[-1] > e[-3]
    above = closes[-1] > e[-1]
    higher = closes[-1] > closes[-lookback - 1]
    return bool(ema_rising and above and higher)


# ── 2. Pullback to support ─────────────────────────────────────────────────

def prior_swing_high(candles: Sequence, *, exclude_last: int = 3) -> Optional[float]:
    """Highest high BEFORE the recent bars (the level being retested)."""
    window = list(candles[: max(0, len(candles) - exclude_last)])
    if len(window) < 5:
        return None
    return max(_v(c, "h") for c in window)


@dataclass(frozen=True)
class SupportTouch:
    level: str      # "ema" | "swing_high"
    support: float
    distance_pct: float  # signed % of latest close vs support (negative = below)


def at_support(
    candles: Sequence,
    *,
    atr: float,
    ema_p: int = 21,
    tolerance_atr: float = 0.75,
) -> Optional[SupportTouch]:
    """Whether the latest close sits AT a support level within tolerance.

    Supports tested (priority order): the rising ``ema_p``; the prior swing
    high (breakout retest). Tolerance is ``tolerance_atr × ATR`` — price may be
    a little above OR below support. Returns the touch when within range.
    """
    if len(candles) < ema_p + 2 or atr <= 0:
        return None
    closes = [_v(c, "c") for c in candles]
    last = closes[-1]
    tol = tolerance_atr * atr

    e = ema(closes, ema_p)
    ema_support = e[-1]
    if math.isfinite(ema_support) and e[-1] > e[-3]:
        dist = last - ema_support
        if abs(dist) <= tol and dist <= tol:
            return SupportTouch("ema", ema_support, dist / last * 100.0)

    swing = prior_swing_high(candles)
    if swing is not None:
        dist = last - swing
        if abs(dist) <= tol:
            return SupportTouch("swing_high", swing, dist / last * 100.0)
    return None


def corrected_from_high(candles: Sequence, *, min_pull_pct: float = 0.015) -> bool:
    """There IS a pullback: latest close is >= min_pull_pct under the recent
    high, so we are not chasing a top. The high uses a small trailing window."""
    if len(candles) < 6:
        return False
    recent = candles[-12:]
    hi = max(_v(c, "h") for c in recent)
    last = _v(candles[-1], "c")
    if hi <= 0:
        return False
    return (hi - last) / hi >= min_pull_pct


# ── 3. Stop-decline + fresh up-bar confirmation ────────────────────────────

def down_leg_losing_steam(candles: Sequence) -> bool:
    """The decline is contracting: the latest down-range is smaller than the
    prior down-range and price set a higher low vs the previous bar's low."""
    if len(candles) < 4:
        return False
    def bear_range(c):
        o, cl = _v(c, "o"), _v(c, "c")
        return max(0.0, o - cl)
    last, prev = candles[-1], candles[-2]
    contracting = bear_range(last) < bear_range(prev)
    higher_low = _v(last, "l") > _v(prev, "l")
    return bool(contracting or higher_low)


def fresh_up_bar(candles: Sequence) -> bool:
    """A fresh turn-up: latest bar is bullish and closes above the prior close."""
    if len(candles) < 2:
        return False
    last = candles[-1]
    o, cl = _v(last, "o"), _v(last, "c")
    return bool(cl > o and cl > _v(candles[-2], "c"))


# ── Composite signal ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PullbackSignal:
    valid: bool
    entry_px: float
    stop_px: float
    support: str
    reason: str


def pullback_entry(
    candles: Sequence,
    *,
    atr: float,
    trend_ema_p: int = 50,
    support_ema_p: int = 21,
    stop_atr_mult: float = 1.5,
    min_pull_pct: float = 0.015,
    tolerance_atr: float = 0.75,
) -> PullbackSignal:
    """Full pullback-to-support long signal.

    Requires all of: established uptrend, an actual correction, price AT a
    support zone, the down-leg losing steam, and a fresh up-bar. Stop is placed
    ``stop_atr_mult × ATR`` below entry (and below the support), giving a
    defined tight risk. Invalid at any step → valid=False with the reason.
    """
    last = _v(candles[-1], "c") if candles else 0.0
    if len(candles) < max(trend_ema_p, support_ema_p) + 2 or atr <= 0:
        return PullbackSignal(False, last, 0.0, "", "insufficient data")
    if not established_uptrend(candles, trend_ema_p=trend_ema_p):
        return PullbackSignal(False, last, 0.0, "", "no established uptrend")
    if not corrected_from_high(candles, min_pull_pct=min_pull_pct):
        return PullbackSignal(False, last, 0.0, "", "no real pullback (still at high)")
    touch = at_support(candles, atr=atr, ema_p=support_ema_p,
                       tolerance_atr=tolerance_atr)
    if touch is None:
        return PullbackSignal(False, last, 0.0, "", "not at a support zone")
    if not down_leg_losing_steam(candles):
        return PullbackSignal(False, last, 0.0, touch.level,
                              "decline still accelerating")
    if not fresh_up_bar(candles):
        return PullbackSignal(False, last, 0.0, touch.level,
                              "no fresh up-bar confirmation")
    stop_px = min(last - stop_atr_mult * atr, touch.support - 0.5 * atr)
    return PullbackSignal(True, last, stop_px, touch.level,
                          f"pullback to {touch.level} + confirmed turn")
