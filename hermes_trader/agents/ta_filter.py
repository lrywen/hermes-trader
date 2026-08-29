"""Pre-AI technical analysis filter.

Performs pure statistical validation of triggered signals before AI analysis.
"""

from __future__ import annotations

import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from hermes_trader.indicators.math import adx, atr, candle_val, ema, obv, rsi
from hermes_trader.agents.perception import extract_fired_triggers
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.models.types import Candle

logger = logging.getLogger(__name__)

# Per-fetch timeout for the parallel candle gather (H9). Without it a single
# hung HTTP call blocks the whole TA layer indefinitely. Mirrors research.py.
_FETCH_TIMEOUT_S = float(os.environ.get("HERMES_TA_FILTER_FETCH_TIMEOUT_S", "45"))


def _assess_trend(candles: list[Candle]) -> str:
    """Bullish / bearish / flat based on EMA8/21 cross and slope."""
    if len(candles) < 30:
        return "flat"

    closes = [candle_val(c, "c") for c in candles]
    ema8_arr = ema(closes, 8)
    ema21_arr = ema(closes, 21)

    i = len(closes) - 1
    e8, e21 = ema8_arr[i], ema21_arr[i]
    if not math.isfinite(e8) or not math.isfinite(e21):
        return "flat"

    e8_prev = ema8_arr[max(0, i - 3)]
    ema_cross = e8 > e21
    slope_rising = e8 > e8_prev

    if ema_cross and slope_rising:
        return "bullish"
    if not ema_cross and not slope_rising:
        return "bearish"
    return "flat"


def _compute_atr4pct(candles: list[Candle]) -> Optional[float]:
    if len(candles) < 20:
        return None
    atr_arr = atr(candles, 14)
    last = atr_arr[-1]
    last_close = candle_val(candles[-1], "c")
    if not math.isfinite(last) or last_close == 0:
        return None
    return (last / last_close) * 100


def _compute_rsi(candles: list[Candle]) -> Optional[float]:
    if len(candles) < 20:
        return None
    arr = rsi(candles, 14)
    last = arr[-1]
    return last if math.isfinite(last) else None


def _compute_adx(candles: list[Candle]) -> Optional[float]:
    if len(candles) < 30:
        return None
    arr = adx(candles, 14)
    last = arr[-1]
    return last if math.isfinite(last) else None


def _check_volume_confirm(candles: list[Candle], mult: float = 1.2) -> bool:
    if len(candles) < 21:
        return False
    last_vol = candle_val(candles[-1], "v")
    avg_vol = sum(candle_val(c, "v") for c in candles[-21:-1]) / 20
    # Volume confirmation requires a real surge: last bar >= 1.2x the prior
    # 20-bar average. The old 0.8x threshold rubber-stamped below-average bars
    # as "confirmed", which let weak breakouts through.
    return avg_vol == 0 or last_vol >= avg_vol * mult


def _extension_atr(candles: list[Candle]) -> Optional[float]:
    """Distance of the last close from EMA21, measured in ATR(14) units.

    Positive = price above EMA21 (extended long / late to chase up),
    negative = below (extended short). Used to reject late momentum bursts.
    """
    if len(candles) < 30:
        return None
    closes = [candle_val(c, "c") for c in candles]
    ema21_arr = ema(closes, 21)
    atr_arr = atr(candles, 14)
    e21 = ema21_arr[-1]
    a = atr_arr[-1]
    last_close = closes[-1]
    if not all(math.isfinite(x) for x in (e21, a, last_close)) or a <= 0:
        return None
    return (last_close - e21) / a


def _obv_slope(candles: list[Candle], lookback: int = 10) -> Optional[float]:
    """OBV change over the last `lookback` bars (signed).

    Positive = accumulation (volume on up-closes), negative = distribution.
    Used to confirm that a breakout/impulse is backed by genuine participation
    rather than a thin push. None if not enough data.
    """
    if len(candles) < lookback + 2:
        return None
    series = obv(candles)
    return series[-1] - series[-lookback - 1]


def _check_ema_cross_recent(candles: list[Candle]) -> bool:
    if len(candles) < 25:
        return False
    closes = [candle_val(c, "c") for c in candles]
    ema8_arr = ema(closes, 8)
    ema21_arr = ema(closes, 21)

    for i in range(len(closes) - 3, len(closes)):
        if i < 1:
            continue
        prev8, prev21 = ema8_arr[i - 1], ema21_arr[i - 1]
        curr8, curr21 = ema8_arr[i], ema21_arr[i]
        if not all(math.isfinite(x) for x in (prev8, prev21, curr8, curr21)):
            continue
        if (prev8 <= prev21 and curr8 > curr21) or (prev8 >= prev21 and curr8 < curr21):
            return True
    return False


def late_entry_check(
    candles_4h: Optional[list[Candle]],
    candles_15m: Optional[list[Candle]],
    side: str,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Shared late-entry veto — the SINGLE source of truth used by the
    pre-AI ta_filter screen, the pre-trade ``ta_late_entry_gate`` in the
    risk-gate chain, and the backtest engine (deep audit ta_filter 高危项,
    2026-08-30). Three call sites, one rule: they can never drift apart.

    Rules (all thresholds come from *params*; the fallbacks below match the
    legacy hardcoded ta_filter values and the CANONICAL_DEFAULTS block):

      1. Over-extension veto (OR semantics, as before): for a LONG, block when
         4h RSI > 75 OR price is > 2.5xATR above EMA21; mirror for shorts.
      2. Trend exception: when 4h ADX >= 35 and the EMA8/21 trend points in
         the trade's direction, widen the limits to RSI 82/18 and ±3.5xATR so
         mid-trend continuation in a strong one-sided move is not mistaken
         for a top/bottom tick.
      3. Multi-timeframe override: if the 4h flags but the 15m RSI has not
         reached its own extreme (< 72 long / > 28 short), the small frame
         still has impulse room — pass the continuation entry. When
         ``candles_15m`` is None the sub-check is "not applicable" for that
         call (the gate fetches 15m; the pre-screen does not).

    Returns a dict: ``block`` (bool), ``reason`` (str), plus the measured
    indicator values (``rsi4h``/``adx4h``/``extension``/``rsi15m``),
    ``relaxed_by_trend``, ``mtf_passed`` (True/False/None=NA) and
    ``data_ok`` (False → not enough 4h data; callers fail OPEN).
    """
    p: dict[str, Any] = params or {}

    def _p(key: str, default: Any) -> Any:
        v = p.get(key, default)
        return default if v is None else v

    result: dict[str, Any] = {
        "block": False,
        "reason": "",
        "rsi4h": None,
        "adx4h": None,
        "extension": None,
        "rsi15m": None,
        "relaxed_by_trend": False,
        "mtf_passed": None,
        "trend_direction": "flat",
        "data_ok": False,
    }
    if side not in ("long", "short"):
        result["reason"] = f"unknown side {side!r}"
        return result

    min_bars_4h = int(_p("min_bars_4h", 30))
    if not candles_4h or len(candles_4h) < min_bars_4h:
        result["reason"] = "insufficient 4h candle data"
        return result
    result["data_ok"] = True

    rsi4h = _compute_rsi(candles_4h)
    adx4h = _compute_adx(candles_4h)
    extension = _extension_atr(candles_4h)
    result["rsi4h"] = rsi4h
    result["adx4h"] = adx4h
    result["extension"] = extension

    is_long = side == "long"
    trend_dir = _assess_trend(candles_4h)
    result["trend_direction"] = trend_dir

    # ── Rule 2: trend exception — widen limits in a strong aligned trend ──
    relaxed = False
    if bool(_p("trend_relax_enabled", True)) and adx4h is not None:
        adx_floor = float(_p("adx_trend_threshold", 35))
        aligned = (trend_dir == "bullish") if is_long else (trend_dir == "bearish")
        if adx4h >= adx_floor and aligned:
            relaxed = True
    result["relaxed_by_trend"] = relaxed

    if is_long:
        rsi_limit = float(_p("rsi_ob_relaxed", 82)) if relaxed else float(_p("rsi_ob", 75))
        ext_limit = float(_p("ext_ob_relaxed", 3.5)) if relaxed else float(_p("ext_ob", 2.5))
        rsi_hit = rsi4h is not None and rsi4h > rsi_limit
        ext_hit = extension is not None and extension > ext_limit
    else:
        rsi_limit = float(_p("rsi_os_relaxed", 18)) if relaxed else float(_p("rsi_os", 25))
        ext_limit = float(_p("ext_os_relaxed", -3.5)) if relaxed else float(_p("ext_os", -2.5))
        rsi_hit = rsi4h is not None and rsi4h < rsi_limit
        ext_hit = extension is not None and extension < ext_limit

    # ── Rule 3 prep: compute 15m RSI whenever data is available. Done before
    # the not-stretched early return so the shadow log / gate always record the
    # small-frame reading (cheap RSI; needed for shadow-period analysis).
    rsi15m: Optional[float] = None
    mtf_passed: Optional[bool] = None
    mtf_enabled = bool(_p("mtf_enabled", True))
    min_bars_15m = int(_p("min_bars_15m", 20))
    if mtf_enabled and candles_15m is not None and len(candles_15m) >= min_bars_15m:
        rsi15m = _compute_rsi(candles_15m)
        result["rsi15m"] = rsi15m
        if rsi15m is not None:
            # Long: 15m not yet overbought (< 72) → still room to push.
            # Short: 15m not yet oversold (> 28) → still room to fall.
            mtf_passed = (
                rsi15m < float(_p("rsi15m_ob", 72))
                if is_long
                else rsi15m > float(_p("rsi15m_os", 28))
            )
            result["mtf_passed"] = mtf_passed

    if not (rsi_hit or ext_hit):
        return result

    why = []
    if rsi_hit:
        why.append(f"RSI {rsi4h:.0f}{'>' if is_long else '<'}{rsi_limit:.0f}")
    if ext_hit:
        why.append(f"extension {'+' if extension > 0 else ''}{extension:.1f}xATR")

    if mtf_passed is True:
        result["reason"] = (
            f"late {side} 4h stretched ({', '.join(why)}) but 15m RSI "
            f"{rsi15m:.0f} not extreme — MTF continuation override"
            f"{' (trend-relaxed)' if relaxed else ''}"
        )
        return result

    suffix = []
    if relaxed:
        suffix.append("strong trend, relaxed limits still exceeded")
    if mtf_enabled and mtf_passed is not True:
        if mtf_passed is False:
            suffix.append(f"15m RSI {rsi15m:.0f} confirms exhaustion")
        else:
            suffix.append("15m override unavailable")
    result["block"] = True
    result["reason"] = f"late {side} ({', '.join(why)})" + (
        f" [{'; '.join(suffix)}]" if suffix else ""
    )
    return result


def _late_entry_params() -> dict[str, Any]:
    """ta_late_entry config block for the shared late-entry veto.

    Late import keeps ta_filter importable without the config layer (unit
    tests pass params explicitly). Canonical defaults match the pure
    function's fallbacks, so behaviour is identical with no config file.
    """
    try:
        from hermes_trader.agents.config_store import cfg_get

        block = cfg_get("ta_late_entry", default={}) or {}
    except Exception:  # noqa: BLE001 — config layer must never break TA
        block = {}
    return block if isinstance(block, dict) else {}


def analyze_perception(perception: dict[str, Any]) -> dict[str, Any]:
    """Run TA validation on a single perception, returning a TA-result dict."""
    coin = perception["coin"]
    try:
        # Parallel fetch for 3 timeframes — counts MUST match research.py
        # so hl_client's 90s candle cache is shared (key = coin|interval|count).
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_1h = pool.submit(fetch_hl_candles, coin, "1h", 100)
            f_4h = pool.submit(fetch_hl_candles, coin, "4h", 100)
            f_1d = pool.submit(fetch_hl_candles, coin, "1d", 60)
            try:
                c1h = f_1h.result(timeout=_FETCH_TIMEOUT_S)
                c4h = f_4h.result(timeout=_FETCH_TIMEOUT_S)
                c1d = f_1d.result(timeout=_FETCH_TIMEOUT_S)
            except Exception:
                for _f in (f_1h, f_4h, f_1d):
                    if not _f.done():
                        _f.cancel()
                raise

        if len(c4h) < 30:
            return {
                "signal": "REJECTED", "score": 0,
                "trend1h": "flat", "trend4h": "flat", "trend1d": "flat",
                "trend_aligned": False,
                "rsi4h": None, "atr4pct": None, "adx4h": None,
                "ema_cross": False, "volume_confirm": False,
                "extension_atr": None,
                "reason": "insufficient candle data",
            }

        t1h = _assess_trend(c1h)
        t4h = _assess_trend(c4h)
        t1d = _assess_trend(c1d)

        is_bullish = t4h == "bullish" or t1d == "bullish"
        is_bearish = t4h == "bearish" or t1d == "bearish"
        # Direction of the higher-timeframe trend. Our measured edge is
        # LONG/trend-aligned (ledger: longs in up-regimes win; shorts are weak),
        # so the CONFIRMED gate should NOT treat a clean downtrend the same as a
        # clean uptrend — the old `is_bullish or is_bearish` awarded the full
        # alignment bonus to both, sending strong-downtrend coins to paid AI as
        # "confirmed" with equal weight. Now: bullish gets full credit, bearish
        # gets partial (a short is tradeable but lower-edge), conflicting = none.
        if is_bullish and not is_bearish:
            trend_direction = "bullish"
        elif is_bearish and not is_bullish:
            trend_direction = "bearish"
        else:
            trend_direction = "flat"
        trend_aligned = trend_direction in ("bullish", "bearish")

        rsi4h = _compute_rsi(c4h)
        atr4pct = _compute_atr4pct(c4h)
        adx4h = _compute_adx(c4h)
        ema_cross = _check_ema_cross_recent(c4h)
        volume_confirm = _check_volume_confirm(c4h)
        extension = _extension_atr(c4h)
        obv_slope = _obv_slope(c4h, lookback=10)

        # Infer the intended trade direction from the fired perception triggers,
        # so the over-extension veto knows which way we'd be chasing.
        # P2-6: canonical extraction via the shared helper.
        fired_names = set(extract_fired_triggers(perception))
        bullish_triggers = fired_names & {
            "breakout", "momentumBurst", "uptrendMomentum",
            "trendFlip1h", "higherLows1h", "volumeBuildup1h", "dailyMover",
        }
        # momentumBurst is direction-agnostic by name; disambiguate using the
        # extension sign (price above EMA21 => the burst was up).
        burst_up = "momentumBurst" in fired_names and (extension or 0) > 0
        burst_down = "momentumBurst" in fired_names and (extension or 0) < 0
        intend_long = bool(bullish_triggers) and not burst_down
        intend_short = bool(fired_names & {"downtrendMomentum"}) or burst_down

        score = 0
        reasons = []

        # Hard late-entry veto via the shared pure function (deep audit
        # ta_filter 高危项, 2026-08-30): the SAME late_entry_check runs as the
        # pre-trade ta_late_entry_gate and in the backtest engine, so the
        # screen / live gate / backtest can never drift apart. The screen
        # fetches no 15m candles, so the MTF continuation override is marked
        # "not applicable" here; the gate re-checks at order time with 15m.
        # Copy: cfg_get may return the live/canonical config dict itself —
        # mutating it would permanently flip mtf_enabled in the shared config.
        le_params = dict(_late_entry_params())
        le_params["mtf_enabled"] = False
        le_side = "long" if intend_long else "short" if intend_short else None
        if le_side is not None:
            le = late_entry_check(c4h, None, le_side, le_params)
            if le.get("block"):
                logger.info(f"[ta_filter] {coin} -> REJECTED ({le['reason']})")
                return {
                    "signal": "REJECTED", "score": 0,
                    "trend1h": t1h, "trend4h": t4h, "trend1d": t1d,
                    "trend_aligned": trend_aligned,
                    "rsi4h": rsi4h, "atr4pct": atr4pct, "adx4h": adx4h,
                    "ema_cross": ema_cross, "volume_confirm": volume_confirm,
                    "extension_atr": extension,
                    "reason": le["reason"],
                }

        # Directional alignment scoring (our edge is LONG/trend-aligned):
        # bullish HTF trend = full +20; bearish = +10 (tradeable short, lower edge);
        # flat/conflicting = 0. This stops the filter from rubber-stamping
        # strong-downtrend coins as equally "confirmed".
        if trend_direction == "bullish":
            score += 20
            reasons.append("trend aligned (bullish)")
        elif trend_direction == "bearish":
            score += 10
            reasons.append("trend aligned (bearish)")
        if rsi4h is not None and 30 < rsi4h < 70:
            score += 15
            reasons.append(f"RSI {rsi4h:.0f}")
        if atr4pct is not None and atr4pct >= 0.5:
            score += 15
            reasons.append(f"ATR {atr4pct:.1f}%")
        if adx4h is not None and adx4h >= 25:
            score += 15
            reasons.append(f"ADX {adx4h:.0f}")
        if ema_cross:
            score += 10
            reasons.append("EMA cross")
        if volume_confirm:
            score += 10
            reasons.append("volume confirmed")
        # OBV direction confirmation: real accumulation/distribution behind
        # the intended move. +8 when OBV slope agrees with the trade direction;
        # a divergent slope (price up but OBV down for a long) is a warning sign
        # but not a hard veto, so it just withholds the bonus.
        if obv_slope is not None:
            if intend_long and obv_slope > 0:
                score += 8
                reasons.append("OBV accumulation")
            elif intend_short and obv_slope < 0:
                score += 8
                reasons.append("OBV distribution")
        score += min(15, perception["composite_score"] / 100 * 15)

        verdict = "CONFIRMED" if score >= 22 else "WEAK" if score >= 12 else "REJECTED"

        if verdict != "CONFIRMED":
            logger.info(f"[ta_filter] {coin} -> {verdict} (score {score:.0f}) reasons: {', '.join(reasons) or 'none'}")

        return {
            "signal": verdict,
            "score": min(100, score),
            "trend1h": t1h, "trend4h": t4h, "trend1d": t1d,
            "trend_aligned": trend_aligned,
            "rsi4h": rsi4h, "atr4pct": atr4pct, "adx4h": adx4h,
            "ema_cross": ema_cross, "volume_confirm": volume_confirm,
            "extension_atr": extension,
            "reason": ", ".join(reasons) if reasons else "no signals",
        }
    except Exception as err:
        # Candle fetches hit the network; a failure rejects the candidate
        # (no-trade is the safe direction) and surfaces the cause.
        return {
            "signal": "REJECTED", "score": 0,
            "trend1h": "flat", "trend4h": "flat", "trend1d": "flat",
            "trend_aligned": False,
            "rsi4h": None, "atr4pct": None, "adx4h": None,
            "ema_cross": False, "volume_confirm": False,
            "extension_atr": None,
            "reason": f"TA error: {err}",
        }
