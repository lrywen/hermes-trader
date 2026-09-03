"""Trigger detection over OHLCV candles.

Computes pct-move spike, volume spike, breakout, range compression and
trend strength, plus a weighted composite score across them.
"""

from __future__ import annotations

import math
from typing import Optional

from hermes_trader.indicators.math import adx, atr, candle_val, ema, sma
from hermes_trader.models.types import Candle, TriggerHit


def pct_move_spike(candles: list[Candle], sigma_threshold: float = 3) -> TriggerHit:
    """Current-bar return z-score vs trailing 96-bar std."""
    if len(candles) < 3:
        return {"name": "pctMoveSpike", "score": 0, "reason": "flat", "fired": False}

    returns = []
    for i in range(1, len(candles)):
        returns.append((candle_val(candles[i], "c") - candle_val(candles[i - 1], "c")) / candle_val(candles[i - 1], "c"))

    current_return = returns[-1]
    prior = returns[:-1][-96:]  # up to 96 trailing bars

    if len(prior) < 2:
        return {"name": "pctMoveSpike", "score": 0, "reason": "flat", "fired": False}

    mean = sum(prior) / len(prior)
    variance = sum((v - mean) ** 2 for v in prior) / len(prior)
    std = variance ** 0.5

    if std == 0:
        return {"name": "pctMoveSpike", "score": 0, "reason": "flat", "fired": False}

    z_score = abs(current_return - mean) / std
    fired = z_score >= sigma_threshold
    score = min(10, max(0, z_score))
    direction = "up" if current_return > mean else "down"

    return {
        "name": "pctMoveSpike",
        "score": score if fired else 0,
        "reason": f"{z_score:.1f}σ return spike {direction}" if fired else "flat",
        "fired": fired,
    }


def volume_spike(candles: list[Candle], sigma_threshold: float = 3) -> TriggerHit:
    """Current volume z-score vs 20-bar rolling window."""
    vols = [candle_val(c, "v") for c in candles]
    if len(vols) < 21:
        return {"name": "volumeSpike", "score": 0, "reason": "flat", "fired": False}

    window = vols[-21:-1]
    current_vol = vols[-1]

    # Skip if >50% of volume samples are 0 (sparse market)
    zero_count = sum(1 for v in window if v == 0)
    if zero_count > len(window) * 0.5:
        return {"name": "volumeSpike", "score": 0, "reason": "sparse", "fired": False}

    mean = sum(window) / len(window)
    variance = sum((v - mean) ** 2 for v in window) / len(window)
    std = variance ** 0.5

    if std == 0:
        return {"name": "volumeSpike", "score": 0, "reason": "flat", "fired": False}

    z_score = abs(current_vol - mean) / std
    fired = z_score >= sigma_threshold
    score = min(10, max(0, z_score))

    return {
        "name": "volumeSpike",
        "score": score if fired else 0,
        "reason": f"{z_score:.1f}σ volume spike" if fired else "flat",
        "fired": fired,
    }


def breakout(
    candles: list[Candle],
    lookback: int = 48,
    min_rvol: float = 1.5,
    rvol_window: int = 20,
    atr_period: int = 14,
    atr_score_mult: float = 3.0,
    confirm_bars: int = 2,
) -> TriggerHit:
    """Breakout detection against the prior range high/low over lookback bars.

    A close beyond the prior range only counts as a fired breakout when
    confirmed by relative volume (RVOL >= ``min_rvol`` vs the prior
    ``rvol_window``-bar average) AND price has held beyond the edge for
    ``confirm_bars`` consecutive closed bars. The latter rejects single-bar
    stop-runs / fakeouts that immediately reverse back into the range — a
    single wick close above the high no longer fires. (Set confirm_bars=1 to
    restore the old single-close behavior.)

    Score is ATR-normalized so BTC/ETH and low-price alts are on the same
    scale: ``score = min(10, |close - edge| / ATR * atr_score_mult)``. A
    close that breaks the edge but fails RVOL/confirmation still earns 50%
    of that score (structural interest, weak confirmation) without firing.
    """
    confirm_bars = max(1, int(confirm_bars))
    if len(candles) < lookback + confirm_bars + 1:
        return {"name": "breakout", "score": 0, "reason": "flat", "fired": False}

    current = candles[-1]
    cur_close = candle_val(current, "c")
    # Prior range is the `lookback` bars BEFORE the confirmation window.
    prior_start = len(candles) - lookback - confirm_bars
    prior_end = len(candles) - confirm_bars

    prior_high = float("-inf")
    prior_low = float("inf")
    for i in range(prior_start, prior_end):
        if candle_val(candles[i], "h") > prior_high:
            prior_high = candle_val(candles[i], "h")
        if candle_val(candles[i], "l") < prior_low:
            prior_low = candle_val(candles[i], "l")

    # Confirmation: every bar in the confirm window must close beyond the
    # same edge (up or down). This is what rejects a one-bar fakeout.
    confirm_closes = [candle_val(candles[i], "c")
                      for i in range(len(candles) - confirm_bars, len(candles))]
    held_above = all(c > prior_high for c in confirm_closes)
    held_below = all(c < prior_low for c in confirm_closes)
    first_break_idx = len(candles) - confirm_bars  # bar that first cleared edge

    # RVOL: current volume relative to prior `rvol_window`-bar average.
    # For multi-bar confirmation, measure volume on the bar that FIRST broke
    # the edge (the start of the confirm window) — that is the volume bar
    # that actually matters for confirming institutional participation.
    rvol_ref_idx = min(len(candles) - 1, first_break_idx)
    cur_vol = candle_val(candles[rvol_ref_idx], "v")
    vol_start = max(0, rvol_ref_idx - rvol_window)
    prior_vols = [candle_val(candles[i], "v") for i in range(vol_start, rvol_ref_idx)]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    if avg_vol <= 0:
        rvol = 0.0
    else:
        rvol = cur_vol / avg_vol
    rvol_ok = rvol >= min_rvol

    # ATR for score normalization. Fall back to pct-break if ATR is not
    # available yet (not enough history) so behavior degrades gracefully.
    atr_series = atr(candles, atr_period)
    cur_atr = atr_series[-1] if atr_series else float("nan")
    atr_valid = isinstance(cur_atr, float) and not math.isnan(cur_atr) and cur_atr > 0

    def _break_score(distance_price: float, reference_price: float) -> float:
        if atr_valid:
            return min(10.0, max(0.0, distance_price / cur_atr * atr_score_mult))
        pct = distance_price / reference_price * 100
        return min(10.0, max(0.0, pct))

    if held_above:
        distance = cur_close - prior_high
        full_score = _break_score(distance, prior_high)
        if rvol_ok:
            held_note = f"held {confirm_bars} bars" if confirm_bars > 1 else ""
            return {
                "name": "breakout",
                "score": full_score,
                "reason": (
                    f"breakout above {lookback}-bar high "
                    f"(+{distance/ prior_high * 100:.2f}%, RVOL {rvol:.2f}x"
                    f"{', ' + held_note if held_note else ''})"
                ),
                "fired": True,
            }
        # Close beyond the edge on weak volume — structural interest but no
        # institutional confirmation. Keep partial score for composite input
        # but do NOT fire (the gate won't see it as a trigger).
        return {
            "name": "breakout",
            "score": full_score * 0.5,
            "reason": (
                f"breakout above {lookback}-bar high on weak volume "
                f"(RVOL {rvol:.2f}x < {min_rvol}x) — unconfirmed"
            ),
            "fired": False,
        }

    if held_below:
        distance = prior_low - cur_close
        full_score = _break_score(distance, prior_low)
        if rvol_ok:
            held_note = f"held {confirm_bars} bars" if confirm_bars > 1 else ""
            return {
                "name": "breakout",
                "score": full_score,
                "reason": (
                    f"breakout below {lookback}-bar low "
                    f"(-{distance / prior_low * 100:.2f}%, RVOL {rvol:.2f}x"
                    f"{', ' + held_note if held_note else ''})"
                ),
                "fired": True,
            }
        return {
            "name": "breakout",
            "score": full_score * 0.5,
            "reason": (
                f"breakout below {lookback}-bar low on weak volume "
                f"(RVOL {rvol:.2f}x < {min_rvol}x) — unconfirmed"
            ),
            "fired": False,
        }

    # A single (or not-yet-confirmed) close beyond the edge, or inside range.
    # Award partial structural score for a lone break bar so composite sees
    # the developing setup, but don't fire until confirm_bars have held.
    if cur_close > prior_high:
        distance = cur_close - prior_high
        return {
            "name": "breakout",
            "score": _break_score(distance, prior_high) * 0.5,
            "reason": (
                f"above {lookback}-bar high but not held {confirm_bars} bars "
                f"— unconfirmed fakeout risk"
            ),
            "fired": False,
        }
    if cur_close < prior_low:
        distance = prior_low - cur_close
        return {
            "name": "breakout",
            "score": _break_score(distance, prior_low) * 0.5,
            "reason": (
                f"below {lookback}-bar low but not held {confirm_bars} bars "
                f"— unconfirmed fakeout risk"
            ),
            "fired": False,
        }

    # Inside range: score proportional to distance from nearest edge.
    dist_up = prior_high - cur_close
    dist_down = cur_close - prior_low
    closest = min(dist_up, dist_down)
    range_size = prior_high - prior_low
    score = max(0, (1 - closest / range_size)) * 5 if range_size > 0 else 0

    return {
        "name": "breakout",
        "score": score,
        "reason": "inside range",
        "fired": False,
    }


def range_compression(
    candles: list[Candle],
    bb_length: int = 20,
    bb_std_dev: float = 2,
) -> TriggerHit:
    """Bollinger Band squeeze: current bandwidth percentile vs the last 100 bars."""
    closes = [candle_val(c, "c") for c in candles]
    if len(closes) < bb_length + 1:
        return {"name": "rangeCompression", "score": 0, "reason": "flat", "fired": False}

    mid = sma(closes, bb_length)
    upper = [float("nan")] * len(closes)
    lower = [float("nan")] * len(closes)

    for i in range(len(closes)):
        if not math.isfinite(mid[i]):
            continue
        sum_sq = 0.0
        count = 0
        for j in range(i - bb_length + 1, i + 1):
            if j < 0:
                continue
            sum_sq += (closes[j] - mid[i]) ** 2
            count += 1
        if count < bb_length:
            continue
        sd = (sum_sq / bb_length) ** 0.5
        upper[i] = mid[i] + sd * bb_std_dev
        lower[i] = mid[i] - sd * bb_std_dev

    bandwidths = []
    for i in range(len(closes)):
        if (
            math.isfinite(mid[i])
            and math.isfinite(upper[i])
            and math.isfinite(lower[i])
            and mid[i] != 0
        ):
            bandwidths.append((upper[i] - lower[i]) / abs(mid[i]))

    if len(bandwidths) < 2:
        return {"name": "rangeCompression", "score": 0, "reason": "flat", "fired": False}

    current_bw = bandwidths[-1]
    history = bandwidths[-100:]
    sorted_bw = sorted(history)

    percentile = 0.0
    for i in range(len(sorted_bw)):
        if sorted_bw[i] < current_bw:
            percentile = ((i + 1) / len(sorted_bw)) * 100

    fired = percentile <= 10
    score = 10 * (1 - percentile / 100)

    return {
        "name": "rangeCompression",
        "score": min(10, score) if fired else 0,
        "reason": f"BB squeeze (P{percentile:.0f})" if fired else "BB normal",
        "fired": fired,
    }


def trend_strength(candles: list[Candle], adx_period: int = 14) -> TriggerHit:
    """Trend strength via ADX(14)."""
    if len(candles) < adx_period * 2 + 1:
        return {"name": "trendStrength", "score": 0, "reason": "flat", "fired": False}

    adx_values = adx(candles, adx_period)
    last_adx = adx_values[-1]

    if not math.isfinite(last_adx):
        return {"name": "trendStrength", "score": 0, "reason": "flat", "fired": False}

    fired = last_adx >= 25
    # ADX is non-linear across a trend's lifecycle:
    #   25-30  = early trend (best entry window) -> full score
    #   30-45  = mature trend                    -> standard score
    #   >45    = extended/ late trend            -> HALF score (chase risk)
    # The raw `last_adx / 4` map would keep rewarding higher ADX all the way
    # to 50+, which systematically surfaced coins already at trend exhaustion.
    if last_adx > 45:
        base = min(10, max(0, last_adx / 4)) * 0.5
    elif last_adx >= 25:
        base = min(10, max(0, last_adx / 4))
    else:
        base = 0

    return {
        "name": "trendStrength",
        "score": base if fired else 0,
        "reason": (f"ADX {last_adx:.1f} trending" +
                   (" (late/extended)" if last_adx > 45 else ""))
        if fired else "flat",
        "fired": fired,
    }


def momentum_burst(
    candles: list[Candle],
    lookback: int = 2,
    pct_threshold: float = 4.0,
) -> TriggerHit:
    """Large cumulative price move over the last `lookback` bars.

    Unlike the z-score triggers, this fires on the raw % move regardless of how
    volatile the coin already is — so it still catches an explosive move once it
    is underway, when recent bars have already inflated the trailing std and
    pushed pct_move_spike's bar to fire out of reach.
    """
    if len(candles) < lookback + 1:
        return {"name": "momentumBurst", "score": 0, "reason": "flat", "fired": False}

    start = candle_val(candles[-lookback - 1], "c")
    end = candle_val(candles[-1], "c")
    if start == 0:
        return {"name": "momentumBurst", "score": 0, "reason": "flat", "fired": False}

    move_pct = (end - start) / start * 100
    fired = abs(move_pct) >= pct_threshold
    score = min(10, max(0, abs(move_pct) / pct_threshold * 5))  # 10 at 2x threshold
    direction = "up" if move_pct > 0 else "down"

    return {
        "name": "momentumBurst",
        "score": score if fired else 0,
        "reason": f"{move_pct:+.1f}% over {lookback} bars {direction}" if fired else "flat",
        "fired": fired,
    }


def _sustained_move_pct(candles: list[Candle], lookback: int) -> Optional[float]:
    """% change over the last `lookback` bars (close-to-close). None if too short."""
    if len(candles) < lookback + 1:
        return None
    start = candle_val(candles[-lookback - 1], "c")
    end = candle_val(candles[-1], "c")
    if start == 0:
        return None
    return (end - start) / start * 100


def uptrend_momentum(candles: list[Candle], lookback: int = 72, pct_threshold: float = 5.0) -> TriggerHit:
    """Sustained UPward move over `lookback` bars (default ~6h on 5m).

    Surfaces a coin in a steady uptrend that the fast spike triggers (which need
    a sharp 5m/2-bar move) miss. The symmetric counterpart to downtrend_momentum;
    together they remove the long-bias in surfacing — both directions get a
    sustained-trend signal so the AI sees up-movers (LONG) and down-movers (SHORT)
    alike. Used as a surfacing BYPASS (weight 0), so it never reaches the
    composite gate's denominator."""
    move = _sustained_move_pct(candles, lookback)
    if move is None:
        return {"name": "uptrendMomentum", "score": 0, "reason": "insufficient_history", "fired": False}
    fired = move >= pct_threshold
    score = min(10, max(0, move / pct_threshold * 5)) if pct_threshold > 0 else 0
    return {
        "name": "uptrendMomentum",
        "score": score if fired else 0,
        "reason": f"+{move:.1f}% over {lookback} bars (uptrend)" if fired else "flat",
        "fired": fired,
    }


def downtrend_momentum(candles: list[Candle], lookback: int = 72, pct_threshold: float = 5.0) -> TriggerHit:
    """Sustained DOWNward move over `lookback` bars (default ~6h on 5m).

    The bearish mirror of uptrend_momentum — surfaces a coin in a steady
    downtrend for SHORT research. This is what was missing: the existing weighted
    triggers (breakout/trendStrength/higherLows) are bullish-structured, so a coin
    grinding down -X% scored ~0 and never reached the AI. Surfacing BYPASS
    (weight 0); downstream the AI calls SHORT, the aligned-conf bar + $50M short
    floor + counter-regime gate adjudicate."""
    move = _sustained_move_pct(candles, lookback)
    if move is None:
        return {"name": "downtrendMomentum", "score": 0, "reason": "insufficient_history", "fired": False}
    fired = move <= -pct_threshold
    score = min(10, max(0, abs(move) / pct_threshold * 5)) if pct_threshold > 0 else 0
    return {
        "name": "downtrendMomentum",
        "score": score if fired else 0,
        "reason": f"{move:.1f}% over {lookback} bars (downtrend)" if fired else "flat",
        "fired": fired,
    }


def bearish_reversal_candle(candles: list[Candle], wick_body_ratio: float = 2.0,
                            context_lookback: int = 6, context_pct: float = 1.5) -> TriggerHit:
    """Bearish reversal candlestick at the TOP of a short advance — a shooting star
    (long upper wick rejecting higher prices) or a bearish engulfing bar. Surfaces
    a SHORT.

    Reversal candles only mean something AFTER an up-move, so a preceding advance of
    >= `context_pct` over `context_lookback` bars is required — otherwise every red
    bar in a downtrend would fire. This is the exhaustion/top signal the momentum
    triggers structurally miss. Surfacing signal: the AI + risk gates adjudicate."""
    if len(candles) < context_lookback + 2:
        return {"name": "bearishReversalCandle", "score": 0, "reason": "insufficient_history", "fired": False}
    o, h, l, c = (candle_val(candles[-1], k) for k in ("o", "h", "l", "c"))
    po, pc = candle_val(candles[-2], "o"), candle_val(candles[-2], "c")
    rng = h - l
    if rng <= 0:
        return {"name": "bearishReversalCandle", "score": 0, "reason": "flat", "fired": False}
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    ctx = _sustained_move_pct(candles[:-1], context_lookback)  # advance into the bar
    advanced = ctx is not None and ctx >= context_pct
    shooting_star = (body > 0 and upper_wick >= wick_body_ratio * body
                     and lower_wick <= body and body <= 0.4 * rng)
    bearish_engulf = pc > po and c < o and o >= pc and c <= po  # prior green, cur red engulfs
    if shooting_star:
        pattern, strength = "shooting_star", min(10.0, upper_wick / body * 2.5)
    elif bearish_engulf:
        prior_body = abs(po - pc)
        pattern = "bearish_engulfing"
        strength = min(10.0, abs(o - c) / prior_body * 5.0) if prior_body > 0 else 5.0
    else:
        pattern, strength = None, 0.0
    fired = bool(pattern) and advanced
    return {
        "name": "bearishReversalCandle",
        "score": strength if fired else 0,
        "reason": f"{pattern} after +{ctx:.1f}%" if fired else "flat",
        "fired": fired,
    }


def bullish_reversal_candle(candles: list[Candle], wick_body_ratio: float = 2.0,
                            context_lookback: int = 6, context_pct: float = 1.5) -> TriggerHit:
    """Bullish reversal candlestick at the BOTTOM of a short decline — a hammer
    (long lower wick rejecting lower prices) or a bullish engulfing bar. Surfaces
    a LONG.

    Mirror of bearish_reversal_candle: requires a preceding DECLINE of >=
    `context_pct` over `context_lookback` bars so it fires at exhaustion, not on
    every green bar in an uptrend. Surfacing signal: the AI + risk gates adjudicate."""
    if len(candles) < context_lookback + 2:
        return {"name": "bullishReversalCandle", "score": 0, "reason": "insufficient_history", "fired": False}
    o, h, l, c = (candle_val(candles[-1], k) for k in ("o", "h", "l", "c"))
    po, pc = candle_val(candles[-2], "o"), candle_val(candles[-2], "c")
    rng = h - l
    if rng <= 0:
        return {"name": "bullishReversalCandle", "score": 0, "reason": "flat", "fired": False}
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    ctx = _sustained_move_pct(candles[:-1], context_lookback)  # decline into the bar
    declined = ctx is not None and ctx <= -context_pct
    hammer = (body > 0 and lower_wick >= wick_body_ratio * body
              and upper_wick <= body and body <= 0.4 * rng)
    bullish_engulf = pc < po and c > o and o <= pc and c >= po  # prior red, cur green engulfs
    if hammer:
        pattern, strength = "hammer", min(10.0, lower_wick / body * 2.5)
    elif bullish_engulf:
        prior_body = abs(po - pc)
        pattern = "bullish_engulfing"
        strength = min(10.0, abs(c - o) / prior_body * 5.0) if prior_body > 0 else 5.0
    else:
        pattern, strength = None, 0.0
    fired = bool(pattern) and declined
    return {
        "name": "bullishReversalCandle",
        "score": strength if fired else 0,
        "reason": f"{pattern} after {ctx:.1f}%" if fired else "flat",
        "fired": fired,
    }


def volume_buildup_1h(candles: list[Candle], ratio_threshold: float = 2.5) -> TriggerHit:
    """Notional-volume surge in the last 4h vs the prior 20h baseline.

    Catches accumulation phases where size is loading into a market before
    price has moved much. Empirically present in 8/10 of the +10% movers
    we missed yesterday (HMSTR 10×, SEI 7.6×, DYDX 6.2×, JTO 21×, ...).
    Needs 1h candles; returns flat if input is anything else or too short.
    """
    if len(candles) < 24:
        return {"name": "volumeBuildup1h", "score": 0, "reason": "insufficient_history", "fired": False}
    recent = sum(candle_val(c, "v") * candle_val(c, "c") for c in candles[-4:])
    prior = sum(candle_val(c, "v") * candle_val(c, "c") for c in candles[-24:-4]) / 20
    if prior <= 0:
        return {"name": "volumeBuildup1h", "score": 0, "reason": "no_baseline", "fired": False}
    ratio = (recent / 4) / prior
    fired = ratio >= ratio_threshold
    score = min(10, ratio / ratio_threshold * 5)  # 10 at 2× threshold
    return {
        "name": "volumeBuildup1h",
        "score": score if fired else 0,
        "reason": f"4h vol {ratio:.1f}× prior 20h baseline" if fired else f"vol {ratio:.1f}× (need {ratio_threshold:.1f}×)",
        "fired": fired,
    }


def trend_flip_1h(candles: list[Candle], lookback_bars: int = 3) -> TriggerHit:
    """1h EMA8 crossed above EMA21 within the last `lookback_bars` bars.

    Catches the inflection moment when a slow downtrend turns. By design
    fires only on UP crosses — a counter-regime LONG bypass is most useful
    when the coin's own 1h trend is actually flipping bullish.
    """
    if len(candles) < 30:
        return {"name": "trendFlip1h", "score": 0, "reason": "insufficient_history", "fired": False}
    closes = [candle_val(c, "c") for c in candles]
    e8 = ema(closes, 8)
    e21 = ema(closes, 21)
    if len(e8) < lookback_bars + 1 or len(e21) < lookback_bars + 1:
        return {"name": "trendFlip1h", "score": 0, "reason": "insufficient_history", "fired": False}
    # Look for a cross in the last N bars: prior bar e8<=e21, current bar e8>e21
    for i in range(-lookback_bars, 0):
        prev_diff = e8[i - 1] - e21[i - 1]
        cur_diff = e8[i] - e21[i]
        if prev_diff <= 0 and cur_diff > 0:
            bars_ago = -i
            return {
                "name": "trendFlip1h",
                "score": 8 if bars_ago == 0 else max(4, 8 - bars_ago * 2),
                "reason": f"EMA8/21 cross up {bars_ago}h ago",
                "fired": True,
            }
    return {"name": "trendFlip1h", "score": 0, "reason": "no recent cross", "fired": False}


def higher_lows_1h(candles: list[Candle], required: int = 4) -> TriggerHit:
    """At least `required` of the last 6 1h closes printed a higher low.

    Pure structure signal — accumulation patterns where each pullback
    holds higher than the prior. WLFI and GRASS had 5/5 yesterday before
    their breakouts. Independent of price direction over the window
    (works during consolidation).
    """
    if len(candles) < 7:
        return {"name": "higherLows1h", "score": 0, "reason": "insufficient_history", "fired": False}
    lows = [candle_val(c, "l") for c in candles[-7:]]
    higher = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    fired = higher >= required
    score = min(10, higher / 6 * 10)
    return {
        "name": "higherLows1h",
        "score": score if fired else 0,
        "reason": f"{higher}/6 higher lows" if fired else f"{higher}/6 (need {required})",
        "fired": fired,
    }


def momentum_continuation_1h(
    candles: list[Candle],
    min_trend_pct: float = 8.0,
    max_pullback_pct: float = 6.0,
) -> TriggerHit:
    """Sustained multi-hour uptrend with an orderly pullback (continuation setup).

    LEAK #2 fix: the spike / breakout / burst triggers all fire on FRESH moves, so
    a coin that is already well up over many hours and is now CONSOLIDATING prints
    no signal and gets missed — even while it sits on the 24h movers leaderboard.
    This trigger catches that state: EMA-stacked (ema9 > ema21, price > ema21),
    gained >= `min_trend_pct` over the last ~12 1h bars, and pulled back
    <= `max_pullback_pct` from the window high (orderly rest, not a blow-off top
    and not a breakdown).

    LONG-biased by construction (requires an up move), so it should only be enabled
    when the macro regime is up/neutral; the counter-trend gate is the backstop.
    Needs 1h candles; returns flat if too short.
    """
    if len(candles) < 24:
        return {"name": "momentumContinuation1h", "score": 0, "reason": "insufficient_history", "fired": False}
    closes = [candle_val(c, "c") for c in candles]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    cur = closes[-1]
    window = closes[-12:]
    base = window[0]
    hi = max(window)
    if base <= 0 or hi <= 0:
        return {"name": "momentumContinuation1h", "score": 0, "reason": "flat", "fired": False}
    trend_pct = (cur - base) / base * 100
    pullback_pct = (hi - cur) / hi * 100
    stacked = e9[-1] > e21[-1] and cur > e21[-1]
    fired = stacked and trend_pct >= min_trend_pct and 0 <= pullback_pct <= max_pullback_pct
    score = min(10, max(0, trend_pct / 3)) if fired else 0
    return {
        "name": "momentumContinuation1h",
        "score": score,
        "reason": (f"+{trend_pct:.1f}% 12h uptrend, {pullback_pct:.1f}% pullback, EMA-stacked"
                   if fired else f"trend {trend_pct:+.1f}% / pullback {pullback_pct:.1f}% / stacked={stacked}"),
        "fired": fired,
    }


def composite_score(hits: list[TriggerHit], weights: dict[str, float]) -> float:
    """Weighted composite score from triggered hits, clamped 0-100.

    Normalizes against the sum of ALL trigger weights (not just fired ones),
    so a single max-score trigger cannot alone score 100; co-firing triggers
    score proportionally higher.
    """
    fired_hits = [h for h in hits if h.get("fired")]
    if not fired_hits:
        return 0

    total_weight = sum(weights.values()) or 1
    weighted_sum = sum(h["score"] * weights.get(h["name"], 0) for h in fired_hits)
    raw = (weighted_sum / total_weight) * 10
    return max(0, min(100, raw))
