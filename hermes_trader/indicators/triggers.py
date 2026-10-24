"""Trigger detection over OHLCV candles.

Computes pct-move spike, volume spike, breakout, range compression and
trend strength, plus a weighted composite score across them.
"""

from __future__ import annotations

import math
from typing import Optional

from hermes_trader.indicators.math import adx, atr, candle_val, ema, sma
from hermes_trader.models.types import Candle, TriggerHit

# B8: per-bar return std (fraction) below which prior vol is economically zero.
# 5e-5 = 0.005%/bar — far below even the ~19bp round-trip taker cost, so a
# "spike" measured against such a baseline is noise, not a tradeable signal.
_SPIKE_STD_MIN = 5e-5


def pct_move_spike(candles: list[Candle], sigma_threshold: float = 3) -> TriggerHit:
    """Current-bar return z-score vs trailing 96-bar std."""
    if len(candles) < 3:
        return {"name": "pctMoveSpike", "score": 0, "reason": "flat", "fired": False,
                "z": 0.0, "direction": "flat"}

    returns = []
    for i in range(1, len(candles)):
        prev_close = candle_val(candles[i - 1], "c")
        # B7: a zero/non-positive prior close makes the return undefined
        # (would raise ZeroDivisionError). Fail closed — drop this pair; if no
        # valid return can be built the caller's length guards return flat.
        if prev_close <= 0:
            continue
        returns.append((candle_val(candles[i], "c") - prev_close) / prev_close)

    current_return = returns[-1] if returns else 0.0
    prior = returns[:-1][-96:]  # up to 96 trailing bars

    if not returns or len(prior) < 2:
        return {"name": "pctMoveSpike", "score": 0, "reason": "flat", "fired": False,
                "z": 0.0, "direction": "flat"}

    mean = sum(prior) / len(prior)
    variance = sum((v - mean) ** 2 for v in prior) / len(prior)
    std = variance ** 0.5

    # B8: an exact-zero check misses near-zero prior vol (e.g. std 1.6e-5 on a
    # near-flat low-liquidity coin), which then inflates an ordinary +0.01% bar
    # to a 6σ "spike". Returns are fractions (1e-4 = 0.01%/bar); a per-bar
    # return std below this absolute floor is economically zero volatility, so
    # a z-score against it is meaningless regardless of the (equally tiny) mean.
    if std < _SPIKE_STD_MIN:
        return {"name": "pctMoveSpike", "score": 0, "reason": "flat", "fired": False,
                "z": 0.0, "direction": "flat"}

    z_score = abs(current_return - mean) / std
    fired = z_score >= sigma_threshold
    score = min(10, max(0, z_score))
    # B3 (2026-10-22): direction is the bar's actual up/down move, not the
    # sign of the deviation from the prior-return mean. In a positively-drifting
    # tape a +0.5% bar (below a ~+1% mean) was labelled "down", contradicting
    # momentum_burst (move_pct > 0) and every other trigger's direction.
    direction = "up" if current_return > 0 else "down"

    return {
        "name": "pctMoveSpike",
        "score": score if fired else 0,
        "reason": f"{z_score:.1f}σ return spike {direction}" if fired else "flat",
        "fired": fired,
        # Audit 2026-09-12 (sigma_burst_gate): structured z so downstream
        # gray-release gates need not regex-parse the reason string. Carried
        # whether fired or not; does NOT change score/fired/reason.
        "z": round(float(z_score), 3),
        "direction": direction,
    }


def volume_spike(candles: list[Candle], sigma_threshold: float = 3) -> TriggerHit:
    """Current volume z-score vs 20-bar rolling window."""
    vols = [candle_val(c, "v") for c in candles]
    if len(vols) < 21:
        return {"name": "volumeSpike", "score": 0, "reason": "flat", "fired": False,
                "z": 0.0}

    window = vols[-21:-1]
    current_vol = vols[-1]

    # Skip if >50% of volume samples are 0 (sparse market)
    zero_count = sum(1 for v in window if v == 0)
    if zero_count > len(window) * 0.5:
        return {"name": "volumeSpike", "score": 0, "reason": "sparse", "fired": False,
                "z": 0.0}

    mean = sum(window) / len(window)
    variance = sum((v - mean) ** 2 for v in window) / len(window)
    std = variance ** 0.5

    if std == 0:
        return {"name": "volumeSpike", "score": 0, "reason": "flat", "fired": False,
                "z": 0.0}

    z_score = abs(current_vol - mean) / std
    fired = z_score >= sigma_threshold
    score = min(10, max(0, z_score))

    return {
        "name": "volumeSpike",
        "score": score if fired else 0,
        "reason": f"{z_score:.1f}σ volume spike" if fired else "flat",
        "fired": fired,
        # Audit 2026-09-12 (sigma_burst_gate): structured z (see pctMoveSpike).
        "z": round(float(z_score), 3),
    }


def breakout(
    candles: list[Candle],
    lookback: int = 48,
    min_rvol: float = 1.5,
    rvol_window: int = 20,
    atr_period: int = 14,
    atr_score_mult: float = 3.0,
    confirm_bars: int = 2,
    flow_confirm: float | None = None,
    flow_confirm_min: float = 0.7,
) -> TriggerHit:
    """Breakout detection against the prior range high/low over lookback bars.

    A close beyond the prior range only counts as a fired breakout when
    confirmed by relative volume (RVOL >= ``min_rvol`` vs the prior
    ``rvol_window``-bar average) AND price has held beyond the edge for
    ``confirm_bars`` consecutive closed bars. The latter rejects single-bar
    stop-runs / fakeouts that immediately reverse back into the range — a
    single wick close above the high no longer fires. (Set confirm_bars=1 to
    restore the old single-close behavior.)

    Adaptive confirmation (launch capture): ``flow_confirm`` is a signed,
    directional aggression read in [-1,1] (CVD aligned with the break: + for
    an upside break, − for downside). When it is at least
    ``flow_confirm_min``, one-sided aggressive taking substitutes for
    bar-holding, so a single strong close is accepted — entering at the
    launch bar instead of waiting the full confirmation. ``None`` (no
    microstructure read) preserves the standard ``confirm_bars`` behavior and
    fails safe.

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

    # Adaptive confirmation: strong one-sided flow aligned with an UPSIDE
    # break lets the latest single close act as the whole confirm window;
    # aligned DOWNSIDE flow likewise. We evaluate the held test against the
    # latest close and mark flow-confirmed so the reason stays auditable.
    flow_up = (flow_confirm is not None
               and float(flow_confirm) >= float(flow_confirm_min))
    flow_dn = (flow_confirm is not None
               and float(flow_confirm) <= -float(flow_confirm_min))

    # Prior range is the `lookback` bars BEFORE the confirmation window.
    prior_start = len(candles) - lookback - confirm_bars
    prior_end = len(candles) - confirm_bars

    # B2 (2026-10-22): fail closed on non-finite prior-range edges. NaN
    # compares False against every number, so a NaN high/low is silently
    # excluded from the extremum scan, collapsing the prior range and letting
    # an ordinary close forge a "confirmed breakout" entry. A single NaN in
    # the prior window forces flat; all-finite input is unchanged.
    prior_high = float("-inf")
    prior_low = float("inf")
    for i in range(prior_start, prior_end):
        bar_high = candle_val(candles[i], "h")
        bar_low = candle_val(candles[i], "l")
        if not (math.isfinite(bar_high) and math.isfinite(bar_low)):
            return {"name": "breakout", "score": 0, "reason": "flat", "fired": False}
        if bar_high > prior_high:
            prior_high = bar_high
        if bar_low < prior_low:
            prior_low = bar_low

    # Confirmation: every bar in the confirm window must close beyond the
    # same edge (up or down). This is what rejects a one-bar fakeout.
    confirm_closes = [candle_val(candles[i], "c")
                      for i in range(len(candles) - confirm_bars, len(candles))]
    held_above = all(c > prior_high for c in confirm_closes)
    held_below = all(c < prior_low for c in confirm_closes)
    # Flow-confirmed launch: the latest single close beyond the edge counts
    # (aggressive taking substitutes for the N-bar hold). Still require the
    # close beyond the same edge so direction is unambiguous.
    flow_confirmed_above = bool(flow_up and cur_close > prior_high)
    flow_confirmed_below = bool(flow_dn and cur_close < prior_low)
    held_above = held_above or flow_confirmed_above
    held_below = held_below or flow_confirmed_below
    first_break_idx = len(candles) - confirm_bars  # bar that first cleared edge

    # RVOL: current volume relative to prior `rvol_window`-bar average.
    # For multi-bar confirmation, measure volume on the bar that FIRST broke
    # the edge (the start of the confirm window) — that is the volume bar
    # that actually matters for confirming institutional participation. For a
    # flow-confirmed launch the latest (launch) bar is the relevant volume.
    rvol_ref_idx = min(len(candles) - 1, first_break_idx)
    if flow_confirmed_above or flow_confirmed_below:
        rvol_ref_idx = len(candles) - 1
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
            flow_note = ", flow-confirmed launch" if flow_confirmed_above else ""
            return {
                "name": "breakout",
                "score": full_score,
                "reason": (
                    f"breakout above {lookback}-bar high "
                    f"(+{distance/ prior_high * 100:.2f}%, RVOL {rvol:.2f}x"
                    f"{', ' + held_note if held_note else ''}{flow_note})"
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
            flow_note = ", flow-confirmed launch" if flow_confirmed_below else ""
            return {
                "name": "breakout",
                "score": full_score,
                "reason": (
                    f"breakout below {lookback}-bar low "
                    f"(-{distance / prior_low * 100:.2f}%, RVOL {rvol:.2f}x"
                    f"{', ' + held_note if held_note else ''}{flow_note})"
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

    # B9: a perfectly flat series makes every bandwidth equal, so
    # ``sorted_bw[i] < current_bw`` is never true and percentile defaults to 0
    # -> a false "BB squeeze (P0)" with score 10. When there is no spread in
    # the bandwidth history there is no compression *relative* to anything;
    # return flat rather than emit a phantom squeeze.
    if sorted_bw[-1] - sorted_bw[0] <= 1e-12:
        return {"name": "rangeCompression", "score": 0, "reason": "flat", "fired": False}

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
    #   25-40  = early/mature trend            -> full score
    #   40-50  = extending into exhaustion     -> score ramps 1.0x -> 0.5x
    #   >=50   = late/extended trend           -> HALF score (chase risk)
    # The raw `last_adx / 4` map would keep rewarding higher ADX all the way
    # to 50+, which systematically surfaced coins already at trend exhaustion.
    #
    # 2026-10-24 (indicator-param review): the previous hard branch
    # ``>45 -> x0.5`` created a score cliff — ADX 45.0 scored 10 but 45.1 scored
    # 5, a 0.1-reading swing halving a 0.30-weight trigger, non-monotonically
    # (46 then scored *below* 44). Replace it with a continuous linear taper
    # over [40,50): multiplier 1.0 at <=40, 0.5 at >=50. No new config knob and
    # no weight change; only the discontinuity is removed.
    if last_adx <= 40:
        late_mult = 1.0
    elif last_adx >= 50:
        late_mult = 0.5
    else:
        late_mult = 1.0 - 0.5 * (last_adx - 40) / 10
    base = min(10, max(0, last_adx / 4)) * late_mult

    return {
        "name": "trendStrength",
        "score": base if fired else 0,
        "reason": (f"ADX {last_adx:.1f} trending" +
                   (" (late/extended)" if last_adx > 40 else ""))
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
        return {"name": "momentumBurst", "score": 0, "reason": "flat", "fired": False,
                "direction": "flat"}

    start = candle_val(candles[-lookback - 1], "c")
    end = candle_val(candles[-1], "c")
    if start == 0:
        return {"name": "momentumBurst", "score": 0, "reason": "flat", "fired": False,
                "direction": "flat"}

    move_pct = (end - start) / start * 100
    fired = abs(move_pct) >= pct_threshold
    score = min(10, max(0, abs(move_pct) / pct_threshold * 5))  # 10 at 2x threshold
    direction = "up" if move_pct > 0 else "down"

    return {
        "name": "momentumBurst",
        "score": score if fired else 0,
        "reason": f"{move_pct:+.1f}% over {lookback} bars {direction}" if fired else "flat",
        "fired": fired,
        # P0-1: structured direction so downstream consumers (e.g.
        # research._fired_trigger_direction) need not regex-parse the reason
        # string. Carried whether fired or not; does not change score/fired.
        "direction": direction,
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


def _candle_cadence_matches(candles: list[Candle], interval_minutes: int) -> bool:
    """B6: True when the median inter-candle spacing matches the requested
    cadence (within 1.5×), so a 5m series cannot masquerade as 1h. Uses the
    median to tolerate an occasional gap."""
    ts = [int(candle_val(c, "t")) for c in candles if candle_val(c, "t") > 0]
    if len(ts) < 4:
        return True  # no trustworthy spacing info — don't false-block
    deltas = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if not deltas:
        return False
    median = deltas[len(deltas) // 2]
    expected = interval_minutes * 60_000
    return expected * 0.5 <= median <= expected * 1.5


def volume_buildup_1h(candles: list[Candle], ratio_threshold: float = 2.5,
                      interval_minutes: Optional[int] = None) -> TriggerHit:
    """Notional-volume surge in the last 4h vs the prior 20h baseline.

    Catches accumulation phases where size is loading into a market before
    price has moved much. Empirically present in 8/10 of the +10% movers
    we missed yesterday (HMSTR 10×, SEI 7.6×, DYDX 6.2×, JTO 21×, ...).
    Needs 1h candles; returns flat if input is anything else or too short.

    B6: pass ``interval_minutes=60`` from the caller to enforce the cadence.
    When supplied, the median spacing of the candle timestamps must match it
    (within one cadence); otherwise the "4h vs 20h" label is arithmetically a
    different window (e.g. 20min vs 100min on 5m bars), so the trigger returns
    flat rather than emit a mislabeled signal. ``None`` keeps legacy callers
    working without enforcement.
    """
    if len(candles) < 24:
        return {"name": "volumeBuildup1h", "score": 0, "reason": "insufficient_history", "fired": False}
    if interval_minutes is not None and not _candle_cadence_matches(candles, interval_minutes):
        return {"name": "volumeBuildup1h", "score": 0, "reason": "interval_mismatch", "fired": False}
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
    # Look for a cross in the last N bars: prior bar e8<=e21, current bar e8>e21.
    # B4: scan NEWEST first (-1 .. -N) so two crosses in the window report the
    # most recent one; the old range(-N, 0) iterated oldest-first and returned
    # the stale cross, and bars_ago could never be 0 (so the score-8 branch was
    # dead). bars_ago is 1..N; most recent (1h ago) scores 6.
    for i in range(-1, -lookback_bars - 1, -1):
        prev_diff = e8[i - 1] - e21[i - 1]
        cur_diff = e8[i] - e21[i]
        if prev_diff <= 0 and cur_diff > 0:
            bars_ago = -i
            return {
                "name": "trendFlip1h",
                "score": max(4, 8 - bars_ago * 2),
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


def apply_squeeze_breakout_coupling(hits: list[TriggerHit]) -> None:
    """Mutate `hits` in place: when a Bollinger squeeze (rangeCompression)
    fired alongside a fired breakout, BOOST the breakout hit's score by +2
    (cap 10) and tag the reason. A breakout resolving out of a squeeze is a
    higher-conviction setup than one from already-wide bands (which is more
    often a late chase / stop run). rangeCompression itself carries weight 0,
    so the boost is expressed on the breakout hit instead.

    B1 (2026-10-22): canonical implementation shared by the live scanner and
    the backtest kernel (``agents.perception`` keeps a same-name alias for the
    scripts/tests that import it from there)."""
    squeeze_fired = any(
        (h.get("name") == "rangeCompression") and h.get("fired") for h in hits
    )
    if not squeeze_fired:
        return
    for h in hits:
        if h.get("name") == "breakout" and h.get("fired"):
            h["score"] = min(10.0, float(h.get("score", 0)) + 2.0)
            reason = h.get("reason") or ""
            if "[squeeze-resolved]" not in reason:
                h["reason"] = f"{reason} [squeeze-resolved]".strip()


def build_hits(
    candles: Optional[list[Candle]],
    thresholds: dict,
    weights: dict,
    *,
    candles_1h: Optional[list[Candle]] = None,
    candles_15m: Optional[list[Candle]] = None,
    flow_confirm=None,
    flow_confirm_min: float = 0.7,
    momentum_continuation: Optional[dict] = None,
    candlestick_patterns: Optional[dict] = None,
    daily_mover: Optional[tuple[bool, str]] = None,
) -> tuple[list[TriggerHit], dict]:
    """Build the full trigger-hit list with the exact orchestration the live
    scanner (``agents.perception._scan_single_market``) uses.

    B1 (2026-10-22): single source of truth shared by live perception and the
    backtest kernel (``backtest.signals.evaluate_window``) so the two trigger
    stacks can no longer drift apart.

    Pure: all inputs (candles, resolved config values, pre-computed
    microstructure/daily-mover context) are passed explicitly; this function
    never reads config, the network, or the clock. Every trigger is safe on an
    empty/short series (returns a flat hit), so backtests without 1h/15m feeds
    simply pass ``None``.

    Returns ``(hits, score_weights)`` where ``score_weights`` is the base
    ``weights`` plus the dynamic ``momentumContinuation1h`` entry, mirroring
    the live denominator handling.

    Order is contractually fixed: 11 base hits (pctMoveSpike, volumeSpike,
    breakout, rangeCompression, trendStrength, momentumBurst,
    volumeBuildup1h, trendFlip1h, higherLows1h, uptrendMomentum,
    downtrendMomentum) — squeeze coupling mutates the breakout in place —
    then optional momentumContinuation1h, the two candlestick hits, and the
    terminal dailyMover hit.

    ``daily_mover`` is the pre-computed ``(fired, reason)`` pair; ``None``
    (or a ``(False, "")`` pair) reproduces the "surface disabled" hit.
    """
    candles = candles or []
    candles_1h = candles_1h or []
    candles_15m = candles_15m or []
    mc = momentum_continuation or {}
    cp = candlestick_patterns or {}

    hits: list[TriggerHit] = [
        pct_move_spike(candles, thresholds["sigmaThreshold"]),
        volume_spike(candles, thresholds["sigmaThreshold"]),
        breakout(
            candles,
            thresholds["breakoutLookback"],
            min_rvol=thresholds.get("breakoutMinRvol", 1.5),
            rvol_window=thresholds.get("breakoutRvolWindow", 20),
            atr_score_mult=thresholds.get("breakoutAtrScoreMult", 3.0),
            confirm_bars=thresholds.get("breakoutConfirmBars", 2),
            flow_confirm=flow_confirm,
            flow_confirm_min=float(flow_confirm_min),
        ),
        range_compression(candles_15m, thresholds["bbLength"], thresholds["bbStdDev"]),
        trend_strength(candles_1h, thresholds["adxPeriod"]),
        momentum_burst(candles, thresholds["momentumLookback"], thresholds["momentumPct"]),
        volume_buildup_1h(candles_1h, thresholds.get("volBuildupRatio", 2.5),
                          interval_minutes=60),
        trend_flip_1h(candles_1h, thresholds.get("trendFlipBars", 3)),
        higher_lows_1h(candles_1h, thresholds.get("higherLowsRequired", 4)),
        # Symmetric directional surfacing (weight 0 → no composite-denominator
        # impact). uptrend/downtrend momentum surface a coin in a sustained
        # intraday trend for research REGARDLESS of the bullish-biased composite
        # gate — the down side is what lets us short selloffs (the weighted
        # triggers are all long-structured, so down-movers scored ~0 and never
        # reached the AI). Acts as a bypass below; the AI + aligned-conf bar +
        # short floor + counter-regime gate adjudicate direction/execution.
        uptrend_momentum(candles, thresholds.get("trendMomentumLookback", 72),
                         thresholds.get("trendMomentumPct", 5.0)),
        downtrend_momentum(candles, thresholds.get("trendMomentumLookback", 72),
                           thresholds.get("trendMomentumPct", 5.0)),
    ]

    # Squeeze-breakout coupling: a breakout that resolves out of a Bollinger
    # squeeze is a higher-conviction setup (boost breakout +2, cap 10).
    # rangeCompression carries weight 0 standalone, so the boost is applied
    # to the breakout hit.
    apply_squeeze_breakout_coupling(hits)

    # Momentum-continuation trigger — OFF by default. Catches a coin in a
    # sustained multi-hour uptrend that is now consolidating (already-extended
    # movers that print no fresh 5m spike). Gated so it has ZERO scoring effect
    # when off: only when enabled is the hit appended AND its weight added to
    # the denominator.
    score_weights = weights
    if mc.get("enabled"):
        hits.append(momentum_continuation_1h(
            candles_1h,
            mc.get("min_trend_pct", 8.0),
            mc.get("max_pullback_pct", 6.0),
        ))
        score_weights = {**weights,
                         "momentumContinuation1h": mc.get("weight", 0.4)}

    # Candlestick reversal patterns — OFF by default. Shooting-star /
    # bearish-engulfing (top of an advance → SHORT) and hammer /
    # bullish-engulfing (bottom of a decline → LONG). Surfacing bypass
    # (weight 0). Gated so it's reversible without code.
    if cp.get("enabled"):
        wbr = cp.get("wick_body_ratio", 2.0)
        ctx_lb = int(cp.get("context_lookback", 6))
        ctx_pct = cp.get("context_pct", 1.5)
        hits.append(bearish_reversal_candle(candles, wbr, ctx_lb, ctx_pct))
        hits.append(bullish_reversal_candle(candles, wbr, ctx_lb, ctx_pct))

    # Daily mover surfacing (weight-0 terminal hit). The live scanner computes
    # fired/reason from the market snapshot; a backtest without that context
    # passes None and gets the flat "not a configured 24h mover" hit.
    daily_mover_fired = bool(daily_mover[0]) if daily_mover is not None else False
    daily_mover_reason = daily_mover[1] if daily_mover is not None else ""
    hits.append({
        "name": "dailyMover",
        "score": 10 if daily_mover_fired else 0,
        "reason": daily_mover_reason or "not a configured 24h mover",
        "fired": daily_mover_fired,
    })
    return hits, score_weights


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


def decay_factor(age_ms: float, halflife_ms: Optional[float]) -> float:
    """Exponential setup-age decay factor (roadmap §2).

    Uses true half-life semantics: ``factor = 2 ** (-age / halflife)``, i.e.
    at ``age == halflife`` the weight is halved (0.5), at two half-lives it
    is 0.25, and so on. (The roadmap's literal ``exp(-age/halflife)`` would
    halve the weight at age ``ln(2) * halflife`` rather than at the named
    half-life; using 2^(-age/halflife) makes the configured ``halflife``
    parameter mean exactly what it says.)

    Returns 1.0 (no decay) when the setup has no age yet (``age_ms <= 0``) or
    the trigger is configured not to decay (``halflife_ms`` missing / <= 0 —
    the pulse-style triggers already self-extinguish when velocity falls, so
    an extra age factor would double-penalize them). ``age_ms`` is clamped at
    >= 0 so a slightly out-of-order clock can never inflate weight above 1.
    """
    if halflife_ms is None or halflife_ms <= 0:
        return 1.0
    if age_ms <= 0:
        return 1.0
    return 2.0 ** (-float(age_ms) / float(halflife_ms))


def composite_score_aged(
    hits: list[TriggerHit],
    weights: dict[str, float],
    current_bar_ms: int,
    onset_ms: dict[str, int],
    halflife_ms: dict[str, float],
) -> float:
    """Age-decayed weighted composite score (roadmap §2 "time-decay factor").

    Identical normalization to :func:`composite_score`: the denominator stays
    the sum of ALL nominal trigger weights, so the surfacing-gate calibration
    is unchanged. Only each FIRED hit's numerator contribution is multiplied
    by its setup-age factor ``2 ** (-onset_age / halflife)``. A trigger that has
    been continuously firing since bars ago — a mature breakout / established
    trend, i.e. the stale-high-score late-chase setup — contributes less and
    less the older its FIRST fire is, while a trigger firing on this bar
    (age 0) contributes at full weight exactly as before.

    ``onset_ms`` maps a fired trigger name to the bar_close_ms of the first
    bar on which it fired (maintained by the perception layer across cycles);
    a missing onset means it is new this bar (age 0 → no decay).
    ``halflife_ms`` maps a trigger name to its half-life in ms; a missing or
    <=0 entry means "never decay" for that trigger.
    """
    fired_hits = [h for h in hits if h.get("fired")]
    if not fired_hits:
        return 0
    total_weight = sum(weights.values()) or 1

    def _factor(name: str) -> float:
        start = onset_ms.get(name)
        if start is None:
            return 1.0
        return decay_factor(current_bar_ms - start, halflife_ms.get(name))

    weighted_sum = 0.0
    for h in fired_hits:
        weighted_sum += h["score"] * weights.get(h["name"], 0) * _factor(h["name"])
    raw = (weighted_sum / total_weight) * 10
    return max(0, min(100, raw))
