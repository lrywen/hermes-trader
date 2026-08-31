#!/usr/bin/env python3
"""A/B/C backtest: OLD (pre-optimization) vs STRICT vs DYNAMIC vs REGIME.

Runs all four rule sets on identical historical 1h candles for the same
universe, then compares entry-point quality, win rate, payoff ratio and
expectancy. The REGIME column is the *production candidate* and carries the
three validated A+B+C optimisations described below.

OLD rules (pre-optimization):
  - No RSI extreme veto
  - No extension-over-ATR veto
  - breakout fires on a single close above the high (confirm_bars=1)
  - momentumBurst bypasses any TA verdict (including REJECTED)
  - No OBV confirmation
  - volume confirm threshold 0.8x
  - ADX gives full score at all levels
  - No squeeze-breakout coupling
  - No chop regime

NEW rules (P0/P1/P2):
  - RSI(14) on 4h > 75 veto long / < 25 veto short
  - |close - ema21| / atr > 2.5 veto
  - breakout requires confirm_bars=2 consecutive closes outside
  - momentumBurst only bypasses WEAK, not REJECTED
  - volume confirm threshold 1.2x
  - ADX > 45 halves trend_strength score
  - squeeze-breakout coupling: +2 to breakout when rangeCompression fires
  - Chop regime (ADX < 20 + EMA-neutral) blocks entries without conviction

REGIME column (A+B+C final, validated on 30d top-20 perp, +$218 / ROE 109%):
  - A. Regime score: ADX weight 0.35 -> 0.25; CHOP threshold upper bound
       0.25 -> 0.40 so the CHOP bucket actually fires (0 -> ~47 trades)
  - B. TREND mid-range RSI 40-60 has no directional edge -> halve size
       (size_mult = min(current, 0.5)); bleeds -$270 -> -$117
  - C. Regime-adaptive hard stop: CHOP/NEUTRAL keep 0.4%, TREND and
       STRONG_TREND widen to 0.8% so trending positions survive 1h noise

All four columns use the same DSL two-phase trailing-stop exit, same sizing,
same universe, same candle data — the ONLY difference is the entry gate.

Usage:
    HERMES_BACKTEST=1 python3 scripts/backtest_ab_compare.py
    HERMES_BACKTEST=1 python3 scripts/backtest_ab_compare.py --days 30 --coins 25
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config import get_config
from hermes_trader.agents.market_regime import regime_strength_score as _canonical_regime_score
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import math as ind
from hermes_trader.indicators import triggers as trig
from hermes_trader.models.types import Candle

ROUND_TRIP_FEE_BPS = 5.0
# H-7 (supplemental audit 2026-08-30): realistic cost model. Live IOC orders
# tolerate up to 1.5% adverse slippage on entry / 5.0% on close
# (rate_limit.py max_slippage_pct), and a DSL max_loss stop is a TRIGGER price,
# not a fill price — exits need mid-hold ~4s + oracle confirm + IOC crossing the
# book, so stop-outs pay an extra delay/overshoot penalty. --no-slippage
# restores the old fee-only baseline; --use-memory-slip backfills the per-coin
# measured adverse exit slippage from memory.avg_exit_slip_bps.
DEFAULT_ENTRY_SLIP_BPS = 5.0       # ~typical taker adverse fill on entry
DEFAULT_EXIT_SLIP_BPS = 15.0       # exits are market/stop-driven → wider
DEFAULT_STOP_DELAY_SLIP_BPS = 10.0  # extra adverse bps on max_loss stop-outs


# ---------------------------------------------------------------------------
# Regime score — data-driven per-coin market-state classification
# ---------------------------------------------------------------------------
#
# Each component is normalised to [0, 1] and combined with fixed weights.
# The resulting score replaces the old hard-coded ADX ladder + resonance
# exception path.  Regimes (Plan A final thresholds):
#   STRONG_TREND  score >= 0.70  — wide RSI/ext thresholds, full size
#   TREND         0.55 - 0.70    — moderately wide thresholds; RSI 40-60 half size
#   NEUTRAL       0.40 - 0.55    — standard thresholds (75/25, 2.5 ATR)
#   CHOP          < 0.40         — tight thresholds, half size, MR overlay
#
# Weights (Plan A): ADX was lowered from 0.35 to 0.25 because the dominant
# ADX component was pushing borderline chop bars into NEUTRAL and starving
# the CHOP bucket (which fired 0 trades).  Rebalancing into ATR (0.225) and
# spreading the remaining 0.525 equally across EMA alignment / price
# extension / OBV gives a smoother score distribution and lets CHOP capture
# genuine non-trading tape (now ~47 trades over 30d top-20).

_REGIME_WEIGHTS = {
    "adx": 0.25,
    "atr": 0.225,
    "ema_align": 0.175,
    "price_ext": 0.175,
    "obv": 0.175,
}


@dataclass(frozen=True)
class RegimeParams:
    """Per-regime veto parameters."""
    name: str
    long_thresh: float
    short_thresh: float
    ext_long_thresh: float    # ext_atr > this vetoes long
    ext_short_thresh: float   # ext_atr < -this vetoes short
    size_mult: float
    mr_overlay: bool          # mean-reversion overlay active (chop)


_REGIME_TABLE: Dict[str, RegimeParams] = {
    "STRONG_TREND": RegimeParams(
        name="STRONG_TREND", long_thresh=95.0, short_thresh=5.0,
        ext_long_thresh=3.5, ext_short_thresh=3.5,
        size_mult=1.0, mr_overlay=False,
    ),
    "TREND": RegimeParams(
        name="TREND", long_thresh=85.0, short_thresh=15.0,
        ext_long_thresh=3.0, ext_short_thresh=3.0,
        size_mult=1.0, mr_overlay=False,
    ),
    "NEUTRAL": RegimeParams(
        name="NEUTRAL", long_thresh=75.0, short_thresh=25.0,
        ext_long_thresh=2.5, ext_short_thresh=2.5,
        size_mult=1.0, mr_overlay=False,
    ),
    "CHOP": RegimeParams(
        name="CHOP", long_thresh=68.0, short_thresh=32.0,
        ext_long_thresh=1.8, ext_short_thresh=1.8,
        size_mult=0.5, mr_overlay=True,
    ),
}


def _regime_score(
    window_1h: List[Candle],
    closes_1h: List[float],
    e8: Optional[float],
    e21: Optional[float],
    atr_v: Optional[float],
    adx_v: Optional[float],
    obv_dir: int,
    bullish: bool,
) -> Tuple[float, str]:
    """Compute a [0, 1] trend-strength score and return (score, regime_label).

    Delegates to the canonical ``market_regime.regime_strength_score`` so the
    backtest and production use byte-identical scoring (previously this was a
    70-line copy that drifted from production's weights/thresholds). The
    precomputed indicator arguments are kept for signature compatibility with
    callers but are not used — the canonical version recomputes from candles.
    """
    score = _canonical_regime_score(window_1h)
    if score >= 0.70:
        label = "STRONG_TREND"
    elif score >= 0.55:
        label = "TREND"
    elif score >= 0.40:
        label = "NEUTRAL"
    else:
        label = "CHOP"
    return score, label


# ---------------------------------------------------------------------------
# Trade + DSL (same exit model for both sides)
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    coin: str
    side: str
    entry_bar: int
    entry_px: float
    rsi_at_entry: float
    ext_atr_at_entry: float
    adx_at_entry: float
    notional: float
    exit_bar: int = 0
    exit_px: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""
    blocked_by: str = ""  # for NEW vetoes, non-empty = would have been blocked
    size_mult: float = 1.0  # dynamic-RSI exception can shrink position to 0.5x
    regime_label: str = ""  # regime score classification at entry


@dataclass
class DSL:
    side: str
    entry_px: float
    entry_bar: int
    peak_px: float
    max_loss_pct: float = 2.5
    protect_pct: float = 1.5
    retrace_threshold: float = 0.30
    hard_timeout_bars: int = 180

    def check_bar(self, bar_idx: int, bar: Candle) -> Tuple[bool, float, str]:
        is_long = self.side == "long"
        if bar_idx - self.entry_bar >= self.hard_timeout_bars:
            return True, bar.c, "hard_timeout"
        max_loss_px = (self.entry_px * (1 - self.max_loss_pct / 100) if is_long
                       else self.entry_px * (1 + self.max_loss_pct / 100))
        if is_long and bar.l <= max_loss_px:
            return True, min(max_loss_px, bar.o), f"max_loss {self.max_loss_pct}%"
        if not is_long and bar.h >= max_loss_px:
            return True, max(max_loss_px, bar.o), f"max_loss {self.max_loss_pct}%"
        if is_long:
            peak_profit_pct = (self.peak_px - self.entry_px) / self.entry_px * 100
            if peak_profit_pct >= self.protect_pct:
                profit_range = self.peak_px - self.entry_px
                floor = self.entry_px + profit_range * (1 - self.retrace_threshold)
                if bar.l <= floor:
                    return True, min(floor, bar.o), "trailing_stop"
        else:
            peak_profit_pct = (self.entry_px - self.peak_px) / self.entry_px * 100
            if peak_profit_pct >= self.protect_pct:
                profit_range = self.entry_px - self.peak_px
                ceiling = self.entry_px - profit_range * (1 - self.retrace_threshold)
                if bar.h >= ceiling:
                    return True, max(ceiling, bar.o), "trailing_stop"
        if is_long and bar.h > self.peak_px:
            self.peak_px = bar.h
        if not is_long and bar.l < self.peak_px:
            self.peak_px = bar.l
        return False, 0.0, ""


# ---------------------------------------------------------------------------
# Indicator helpers (computed once per bar, shared by OLD and NEW)
# ---------------------------------------------------------------------------

def _ema_val(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    arr = ind.ema(closes, period)
    v = arr[-1]
    return v if math.isfinite(v) else None


def _atr_val(window: List[Candle], period: int = 14) -> Optional[float]:
    if len(window) < period + 1:
        return None
    arr = ind.atr(window, period)
    v = arr[-1]
    return v if math.isfinite(v) else None


def _adx_val(window: List[Candle], period: int = 14) -> Optional[float]:
    if len(window) < period * 2:
        return None
    arr = ind.adx(window, period)
    for v in reversed(arr):
        if v == v and v != float("inf"):
            return v
    return None


def _obv_slope(window: List[Candle], period: int = 10) -> int:
    """+1 if OBV rising, -1 if falling, 0 if flat/insufficient."""
    if len(window) < period + 1:
        return 0
    obv = [0.0]
    for i in range(1, len(window)):
        if window[i].c > window[i - 1].c:
            obv.append(obv[-1] + window[i].v)
        elif window[i].c < window[i - 1].c:
            obv.append(obv[-1] - window[i].v)
        else:
            obv.append(obv[-1])
    recent = obv[-period:]
    if len(recent) < 2:
        return 0
    xbar = (len(recent) - 1) / 2
    ybar = sum(recent) / len(recent)
    num = sum((i - xbar) * (y - ybar) for i, y in enumerate(recent))
    if num > 0:
        return 1
    if num < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Entry evaluation: returns (verdict, side, rsi, ext_atr, adx, block_reason)
# verdict = "LONG" / "SHORT" / None
# block_reason is non-empty only for NEW rules that veto an otherwise-valid signal
# ---------------------------------------------------------------------------

def _evaluate_entry(
    window_1h: List[Candle],
    window_4h: List[Candle],
    cfg: Dict[str, Any],
    *,
    use_new_rules: bool,
    rsi_variant: str = "strict",
    plan_b_enabled: bool = True,
) -> Tuple[Optional[str], float, float, float, str, float, str]:
    th = cfg["thresholds"]
    closes_1h = [c.c for c in window_1h]
    if len(closes_1h) < 50:
        return None, 50, 0, 0, "", 1.0, ""

    # --- Triggers (shared computation; breakout confirm_bars differs) ---
    confirm_bars = 2 if use_new_rules else 1
    hits = [
        trig.pct_move_spike(window_1h, th["sigmaThreshold"]),
        trig.volume_spike(window_1h, th["sigmaThreshold"]),
        trig.breakout(
            window_1h, th["breakoutLookback"],
            min_rvol=th.get("breakoutMinRvol", 1.5),
            rvol_window=th.get("breakoutRvolWindow", 20),
            atr_score_mult=th.get("breakoutAtrScoreMult", 3.0),
            confirm_bars=confirm_bars,
        ),
        trig.range_compression(window_1h, th["bbLength"], th["bbStdDev"]),
        trig.trend_strength(window_1h, th["adxPeriod"]),
        trig.momentum_burst(window_1h, th["momentumLookback"], th["momentumPct"]),
    ]

    # NEW: squeeze-breakout coupling
    if use_new_rules:
        squeeze_fired = any(
            (h.get("name") == "rangeCompression") and h.get("fired") for h in hits
        )
        if squeeze_fired:
            for h in hits:
                if h.get("name") == "breakout" and h.get("fired"):
                    h["score"] = min(10.0, float(h.get("score", 0)) + 2.0)

    score = trig.composite_score(hits, cfg["weights"])
    burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
    breakout_fired = any(h["name"] == "breakout" and h["fired"] for h in hits)

    # --- Trend direction (EMA8/21 on 1h) ---
    e8 = _ema_val(closes_1h, 8)
    e21 = _ema_val(closes_1h, 21)
    if e8 is None or e21 is None:
        return None, 50, 0, 0, "", 1.0, ""
    bullish = e8 > e21

    # --- ATR% and ADX ---
    atr_v = _atr_val(window_1h)
    atr_pct = (atr_v / closes_1h[-1] * 100) if atr_v else None
    adx_v = _adx_val(window_1h)

    # --- RSI on 4h (NEW veto input; computed but ignored by OLD) ---
    closes_4h = [c.c for c in window_4h] if window_4h else closes_1h
    rsi = ind.rsi_last(closes_4h, 14)
    if rsi is None:
        rsi = ind.rsi_last(closes_1h, 14) or 50.0

    # --- 1d RSI / EMA21 for multi-timeframe resonance (dynamic RSI only) ---
    # Build daily closes by resampling the last 336 1h bars (14 days) into 24h groups.
    rsi_1d: Optional[float] = None
    ema21_1d_prev: Optional[float] = None
    if rsi_variant == "dynamic" and len(closes_1h) >= 24 * 30:
        daily_closes: List[float] = []
        for j in range(0, len(closes_1h) - 23, 24):
            daily_closes.append(closes_1h[j + 23])
        rsi_1d = ind.rsi_last(daily_closes[-60:], 14)
        e_1d = _ema_val(daily_closes, 21)
        if e_1d is not None and len(daily_closes) >= 2:
            ema21_1d_prev = _ema_val(daily_closes[:-1], 21)

    # --- Extension over ATR (NEW veto input) ---
    ext_atr = 0.0
    if atr_v and e21 and atr_v > 0:
        ext_atr = (closes_1h[-1] - e21) / atr_v

    # --- OBV slope (NEW adds confirmation score) ---
    obv_dir = _obv_slope(window_1h) if use_new_rules else 0

    # --- Heuristic verdict (stand-in for AI) ---
    # OLD: score >= 25 OR trend with ATR >= 0.4% OR burst
    # NEW: same base, but OBV alignment adds confidence
    score_ok = score >= 25
    trend_ok = atr_pct is not None and atr_pct >= 0.4
    if not (score_ok or trend_ok or burst):
        return None, rsi, ext_atr, adx_v or 0, "", 1.0, ""

    side = "LONG" if bullish else "SHORT"

    # --- TA confirm proxy ---
    # OLD: loose proxy (see backtest.py _ta_confirmed)
    # NEW: same base + OBV direction bonus + volume threshold 1.2x
    def _old_ta_confirmed() -> bool:
        if atr_pct is None:
            return False
        s = 20
        if 30 < (atr_pct * 10) < 700:
            s += 15
        if atr_pct >= 0.5:
            s += 15
        if adx_v is not None and adx_v >= 25:
            s += 15
        s += min(15, score / 100 * 15)
        return s >= 45

    def _new_ta_confirmed() -> bool:
        if atr_pct is None:
            return False
        s = 20
        if 30 < (atr_pct * 10) < 700:
            s += 15
        if atr_pct >= 0.5:
            s += 15
        if adx_v is not None and adx_v >= 25:
            s += 15
        s += min(15, score / 100 * 15)
        # OBV alignment bonus
        if (side == "LONG" and obv_dir > 0) or (side == "SHORT" and obv_dir < 0):
            s += 8
        # Volume confirm: need 1.2x average (stricter than old 0.8x)
        vol_hit = next((h for h in hits if h["name"] == "volumeSpike"), None)
        if vol_hit and vol_hit.get("fired"):
            s += 10
        return s >= 45

    ta_ok = _new_ta_confirmed() if use_new_rules else _old_ta_confirmed()

    # Dynamic RSI sizing factor (1.0 = normal; 0.5 = exception, half size).
    size_mult = 1.0
    regime_label = ""

    # OLD: burst bypasses EVERYTHING (including would-be REJECTED)
    # NEW: burst only bypasses WEAK (ta_ok=False but not hard-vetoed)
    if not use_new_rules:
        if not ta_ok and not burst:
            return None, rsi, ext_atr, adx_v or 0, "", 1.0, ""
    else:
        # --- NEW hard vetoes (P0) ---
        block = ""

        # RSI veto: strict (fixed 75/25), dynamic (ADX ladder), or regime
        # (data-driven per-coin score).
        if rsi_variant in ("dynamic", "regime"):
            if rsi_variant == "regime":
                regime_score, regime_label = _regime_score(
                    window_1h, closes_1h, e8, e21, atr_v, adx_v, obv_dir, bullish,
                )
                rp = _REGIME_TABLE[regime_label]
                long_thresh = rp.long_thresh
                short_thresh = rp.short_thresh
                ext_long = rp.ext_long_thresh
                ext_short = rp.ext_short_thresh
                size_mult = rp.size_mult
            else:
                # Legacy dynamic: ADX-scaled thresholds with resonance exception.
                adx_for_thresh = adx_v if adx_v is not None else 20.0
                if adx_for_thresh >= 40:
                    long_thresh = 90.0
                    short_thresh = 15.0 if adx_for_thresh >= 45 else 20.0
                elif adx_for_thresh >= 30:
                    long_thresh, short_thresh = 80.0, 20.0
                elif adx_for_thresh < 20:
                    long_thresh, short_thresh = 70.0, 30.0
                else:
                    long_thresh, short_thresh = 75.0, 25.0
                ext_long = 2.5
                ext_short = 2.5

                # 1d resonance for legacy dynamic exception path.
                _daily_closes: List[float] = []
                if len(closes_1h) >= 24 * 30:
                    for j in range(0, len(closes_1h) - 23, 24):
                        _daily_closes.append(closes_1h[j + 23])
                _e1d_now = _ema_val(_daily_closes, 21) if _daily_closes else None
                _e1d_prev = _ema_val(_daily_closes[:-1], 21) if len(_daily_closes) >= 2 else None
                daily_uptrend = _e1d_now is not None and _e1d_prev is not None and _e1d_now > _e1d_prev
                daily_downtrend = _e1d_now is not None and _e1d_prev is not None and _e1d_now < _e1d_prev

                vols_1h = [c.v for c in window_1h[-21:]]
                if len(vols_1h) >= 21:
                    _vmean = sum(vols_1h[:-1]) / 20.0
                    vol_ratio = vols_1h[-1] / _vmean if _vmean > 0 else 1.0
                else:
                    vol_ratio = 1.0

            if side == "LONG":
                if rsi > long_thresh:
                    if rsi_variant == "dynamic":
                        resonance_pass = (rsi_1d is not None and rsi_1d < 75.0 and daily_uptrend)
                        # Dead code fix: RSI cap links to long_thresh instead of
                        # hard-coded 80 (was: rsi <= 80, impossible when thresh=90).
                        cap = min(90.0, long_thresh + 10.0)
                        if resonance_pass and rsi <= cap and vol_ratio >= 1.2 and obv_dir > 0:
                            size_mult = 0.5
                        else:
                            block = f"late long (RSI {rsi:.0f}>{long_thresh:.0f}, ADX {adx_for_thresh:.0f})"
                    else:
                        block = (f"late long (RSI {rsi:.0f}>{long_thresh:.0f}, "
                                 f"regime {regime_label} score {regime_score:.2f})")
                elif ext_atr > ext_long:
                    block = (f"overextended long (+{ext_atr:.1f}xATR, "
                             f"regime {regime_label if rsi_variant == 'regime' else ''})")
            else:  # SHORT
                if rsi < short_thresh:
                    if rsi_variant == "dynamic":
                        resonance_pass = (rsi_1d is not None and rsi_1d > 25.0 and daily_downtrend)
                        cap = max(10.0, short_thresh - 10.0)
                        if resonance_pass and rsi >= cap and vol_ratio >= 1.2 and obv_dir < 0:
                            size_mult = 0.5
                        else:
                            block = f"late short (RSI {rsi:.0f}<{short_thresh:.0f}, ADX {adx_for_thresh:.0f})"
                    else:
                        block = (f"late short (RSI {rsi:.0f}<{short_thresh:.0f}, "
                                 f"regime {regime_label} score {regime_score:.2f})")
                elif ext_atr < -ext_short:
                    block = (f"overextended short ({ext_atr:.1f}xATR, "
                             f"regime {regime_label if rsi_variant == 'regime' else ''})")
        else:
            # Strict (original P0) RSI vetoes.
            if side == "LONG":
                if rsi > 75:
                    block = f"late long (RSI {rsi:.0f}>75)"
                elif ext_atr > 2.5:
                    block = f"overextended long (+{ext_atr:.1f}xATR)"
            else:
                if rsi < 25:
                    block = f"late short (RSI {rsi:.0f}<25)"
                elif ext_atr < -2.5:
                    block = f"overextended short ({ext_atr:.1f}xATR)"

        # --- Chop regime (P2) ---
        # In "regime" mode, CHOP classification is already handled by the
        # regime score (tighter thresholds + size_mult).  Only apply the
        # legacy ADX<20 hard-block for strict/dynamic variants.
        if (not block and rsi_variant != "regime"
                and adx_v is not None and adx_v < 20 and not burst):
            # EMA-neutral + low ADX = chop; require strong conviction
            if score < 55 and not breakout_fired:
                block = f"chop regime (ADX {adx_v:.0f}<20, score {score:.0f}<55)"

        if block:
            # If burst fires, NEW still blocks hard vetoes (RSI/extension)
            # but allows burst through chop
            if "chop" in block and burst:
                pass  # burst escapes chop
            else:
                return None, rsi, ext_atr, adx_v or 0, block, 1.0, regime_label

        if not ta_ok and not burst:
            return None, rsi, ext_atr, adx_v or 0, "", 1.0, regime_label

        # --- Plan B: TREND mid-range RSI 40-60 has no directional edge ---
        # Attribution showed these trades bleed equally on long and short
        # (858/1085 stopped out at 0.4% max_loss).  Halve size to cut risk
        # while keeping signal coverage.
        if (plan_b_enabled and rsi_variant == "regime"
                and regime_label == "TREND" and 40.0 <= rsi < 60.0):
            size_mult = min(size_mult, 0.5)

    return side, rsi, ext_atr, adx_v or 0, "", size_mult, regime_label


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _resample_4h(candles_1h: List[Candle]) -> List[Candle]:
    """Aggregate 1h candles into 4h candles."""
    out: List[Candle] = []
    for i in range(0, len(candles_1h) - 3, 4):
        group = candles_1h[i:i + 4]
        out.append(Candle(
            t=group[0].t,
            o=group[0].o,
            h=max(c.h for c in group),
            l=min(c.l for c in group),
            c=group[-1].c,
            v=sum(c.v for c in group),
        ))
    return out


def _simulate(
    coin: str,
    candles_1h: List[Candle],
    max_lev: int,
    *,
    equity: float,
    equity_fraction: float,
    lev_ceiling: int,
    cfg: Dict[str, Any],
    use_new_rules: bool,
    warmup: int = 120,
    max_loss_pct: float = 2.5,
    protect_pct: float = 1.5,
    retrace_threshold: float = 0.30,
    veto_log: Optional[List[Dict[str, Any]]] = None,
    rsi_variant: str = "strict",
    plan_b_enabled: bool = True,
    entry_slip_bps: float = DEFAULT_ENTRY_SLIP_BPS,
    exit_slip_bps: float = DEFAULT_EXIT_SLIP_BPS,
    stop_delay_slip_bps: float = DEFAULT_STOP_DELAY_SLIP_BPS,
    fee_bps: float = ROUND_TRIP_FEE_BPS,
) -> List[Trade]:
    trades: List[Trade] = []
    open_t: Optional[Trade] = None
    open_dsl: Optional[DSL] = None
    # O-8 (supplemental audit 2026-08-30): fee_bps can be calibrated per coin
    # from memory.avg_round_trip_fee_bps; defaults to the static 5-bps constant.
    fee_pct = fee_bps / 10000.0
    candles_4h = _resample_4h(candles_1h)

    # H-7: adverse fill — a BUY fills above the reference price, a SELL below.
    def _fill(px: float, is_buy: bool, bps: float) -> float:
        adj = px * bps / 10000.0
        return px + adj if is_buy else px - adj

    for i in range(warmup, len(candles_1h) - 1):
        window_1h = candles_1h[: i + 1]
        # 4h window: use all 4h candles up to the current 1h bar's time
        t_now = candles_1h[i].t
        window_4h = [c for c in candles_4h if c.t <= t_now]
        if len(window_4h) < 20:
            window_4h = window_1h  # fallback

        bar = candles_1h[i]
        next_bar = candles_1h[i + 1]

        # Manage open position
        if open_t and open_dsl:
            done, exit_ref, reason = open_dsl.check_bar(i, bar)
            if done:
                # H-7: the DSL stop/exit price is a trigger, not a fill. Apply
                # adverse exit slippage; max_loss stop-outs additionally pay the
                # confirm-delay / order-book-cross penalty. Closing a long is a
                # SELL (is_buy=False); closing a short is a BUY (is_buy=True).
                is_stop_out = reason.startswith("max_loss")
                slip = exit_slip_bps + (stop_delay_slip_bps if is_stop_out else 0.0)
                exit_px = _fill(exit_ref, open_t.side == "short", slip)
                gross_pct = ((exit_px - open_t.entry_px) / open_t.entry_px
                             if open_t.side == "long"
                             else (open_t.entry_px - exit_px) / open_t.entry_px)
                open_t.exit_bar = i
                open_t.exit_px = exit_px
                open_t.pnl_usd = open_t.notional * (gross_pct - fee_pct)
                open_t.exit_reason = reason
                trades.append(open_t)
                open_t = open_dsl = None
            else:
                continue

        verdict, rsi, ext_atr, adx_v, block, size_mult, regime_label = _evaluate_entry(
            window_1h, window_4h, cfg, use_new_rules=use_new_rules,
            rsi_variant=rsi_variant, plan_b_enabled=plan_b_enabled,
        )

        if block and veto_log is not None:
            veto_log.append({
                "coin": coin,
                "bar": i,
                "side": "long" if "long" in block else "short",
                "rsi": round(rsi, 1),
                "ext_atr": round(ext_atr, 2),
                "adx": round(adx_v, 1),
                "reason": block,
                "price": window_1h[-1].c,
            })

        if verdict and size_mult < 1.0 and veto_log is not None:
            # Track regime/dynamic-RSI exception path (reduced-size entries).
            exc_side = "long" if verdict == "LONG" else "short"
            veto_log.append({
                "coin": coin,
                "bar": i,
                "side": exc_side,
                "rsi": round(rsi, 1),
                "ext_atr": round(ext_atr, 2),
                "adx": round(adx_v, 1),
                "reason": f"REGIME/SIZE EXCEPTION (size x{size_mult:.1f})",
                "price": window_1h[-1].c,
            })

        if verdict is None:
            continue

        side = "long" if verdict == "LONG" else "short"
        lev = min(lev_ceiling, max_lev)
        notional = equity * equity_fraction * lev * size_mult
        # --- Plan C: regime-adaptive hard stop ---
        # CHOP/NEUTRAL keep tight 0.4% stop; TREND/STRONG_TREND widen to 0.8%
        # so trending positions aren't shaken out by 1h noise before the
        # trailing protect kicks in.
        eff_max_loss = max_loss_pct
        if rsi_variant == "regime" and regime_label in ("TREND", "STRONG_TREND"):
            eff_max_loss = max(max_loss_pct, 0.8)
        # H-7: fill the next bar's open at an adverse entry price (BUY above,
        # SELL below); anchor the DSL to the actual fill, matching live dsl_exit.
        entry_px = _fill(next_bar.o, side == "long", entry_slip_bps)
        open_t = Trade(
            coin=coin, side=side, entry_bar=i + 1, entry_px=entry_px,
            rsi_at_entry=rsi, ext_atr_at_entry=ext_atr, adx_at_entry=adx_v,
            notional=notional, size_mult=size_mult, regime_label=regime_label,
        )
        open_dsl = DSL(
            side=side, entry_px=entry_px, entry_bar=i + 1,
            peak_px=entry_px, max_loss_pct=eff_max_loss,
            protect_pct=protect_pct, retrace_threshold=retrace_threshold,
        )
    return trades


# ---------------------------------------------------------------------------
# Stats + report
# ---------------------------------------------------------------------------

def _stats(trades: List[Trade], equity: float, days: int, label: str) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0}
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    pnl = sum(t.pnl_usd for t in trades)
    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    expectancy = pnl / n
    wr = len(wins) / n * 100

    # Entry quality: distribution of RSI and extension at entry
    rsi_vals = [t.rsi_at_entry for t in trades]
    ext_vals = [t.ext_atr_at_entry for t in trades]
    adx_vals = [t.adx_at_entry for t in trades]
    late_entries = sum(
        1 for t in trades
        if (t.side == "long" and (t.rsi_at_entry > 75 or t.ext_atr_at_entry > 2.5))
        or (t.side == "short" and (t.rsi_at_entry < 25 or t.ext_atr_at_entry < -2.5))
    )
    exit_reasons: Dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return {
        "label": label,
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "expectancy": expectancy,
        "pnl": pnl,
        "pnl_pct": pnl / equity * 100,
        "rsi_mean": sum(rsi_vals) / n,
        "rsi_median": sorted(rsi_vals)[n // 2],
        "rsi_overbought_pct": sum(1 for r in rsi_vals if r > 75) / n * 100,
        "rsi_oversold_pct": sum(1 for r in rsi_vals if r < 25) / n * 100,
        "ext_mean": sum(ext_vals) / n,
        "ext_abs_mean": sum(abs(e) for e in ext_vals) / n,
        "ext_over_2_5_pct": sum(1 for e in ext_vals if abs(e) > 2.5) / n * 100,
        "adx_mean": sum(adx_vals) / n,
        "late_entry_pct": late_entries / n * 100,
        "exit_reasons": exit_reasons,
        "trades": trades,
    }


def _print_report(
    old: Dict[str, Any],
    strict: Dict[str, Any],
    dyn: Dict[str, Any],
    regime: Dict[str, Any],
    strict_vetoes: List[Dict],
    dyn_vetoes: List[Dict],
    regime_vetoes: List[Dict],
    days: int,
    cost_note: str = "",
) -> None:
    from collections import Counter

    print("\n" + "=" * 110)
    print(f"  A/B/C BACKTEST — OLD vs STRICT vs DYNAMIC vs REGIME — {days} days, 1h bars")
    print("=" * 110)

    cols = [old, strict, dyn, regime]
    labels = [c["label"] for c in cols]
    w = 16

    def _row(metric: str, key: str = "", fmt: str = "{:.1f}", pct: bool = False,
             dollar: bool = False, pp: bool = False) -> None:
        vals = []
        for c in cols:
            if c["n"] == 0 or key not in c:
                vals.append("-")
                continue
            v = c[key]
            if pct:
                vals.append(f"{v:.1f}%")
            elif dollar:
                vals.append(f"${v:+.2f}")
            elif pp:
                vals.append(f"{v:.1f}pp")
            else:
                vals.append(fmt.format(v))
        cells = " ".join(f"{v:>{w}s}" for v in vals)
        print(f"  {metric:<28s} {cells}")

    print(f"\n  {'Metric':<28s} " + " ".join(f"{l:>{w}s}" for l in labels))
    print(f"  {'-'*28} " + " ".join(f"{'-'*w}" for _ in labels))

    if old["n"] == 0:
        print("  Insufficient trades for comparison.")
        return

    _row("Total trades", "n", fmt="{:d}")
    _row("Win rate", "win_rate", pct=True)
    _row("Avg win", "avg_win", dollar=True)
    _row("Avg loss", "avg_loss", dollar=True)
    _row("Payoff ratio", "payoff", fmt="{:.2f}")
    _row("Expectancy/trade", "expectancy", dollar=True)
    _row("Total PnL", "pnl", dollar=True)
    _row("Return on equity", "pnl_pct", pct=True)

    print(f"\n  --- Entry quality (lower RSI extremes / extension = better) ---")
    _row("RSI at entry (mean)", "rsi_mean", fmt="{:.1f}")
    _row("RSI >75 entries", "rsi_overbought_pct", pct=True)
    _row("RSI <25 entries", "rsi_oversold_pct", pct=True)
    _row("|extension| / ATR (mean)", "ext_abs_mean", fmt="{:.2f}")
    _row("|extension| > 2.5 ATR", "ext_over_2_5_pct", pct=True)
    _row("Late entries (RSI+ext)", "late_entry_pct", pct=True)
    _row("ADX at entry (mean)", "adx_mean", fmt="{:.1f}")

    # Reduced-size exception trades for DYNAMIC and REGIME
    for stat, tag in [(dyn, "DYNAMIC"), (regime, "REGIME")]:
        if stat["n"] > 0:
            exc_trades = [t for t in stat["trades"] if t.size_mult < 1.0]
            if exc_trades:
                exc_pnl = sum(t.pnl_usd for t in exc_trades)
                exc_wr = (sum(1 for t in exc_trades if t.pnl_usd > 0)
                          / len(exc_trades) * 100)
                print(f"\n  --- {tag} reduced-size entries (size < 1.0x) ---")
                print(f"    Trades : {len(exc_trades)} ({len(exc_trades)/stat['n']*100:.1f}% of {tag})")
                print(f"    PnL    : ${exc_pnl:+.2f}")
                print(f"    Winrate: {exc_wr:.1f}%")
                print(f"    Avg PnL: ${exc_pnl/len(exc_trades):+.3f}/trade")

    print(f"\n  --- Exit reason distribution ---")
    all_reasons = sorted(set(
        list(old.get("exit_reasons", {}).keys())
        + list(strict.get("exit_reasons", {}).keys())
        + list(dyn.get("exit_reasons", {}).keys())
        + list(regime.get("exit_reasons", {}).keys())
    ))
    for r in all_reasons:
        o = old.get("exit_reasons", {}).get(r, 0)
        s = strict.get("exit_reasons", {}).get(r, 0)
        d = dyn.get("exit_reasons", {}).get(r, 0)
        g = regime.get("exit_reasons", {}).get(r, 0)
        print(f"    {r:<24s} OLD:{o:4d}  STR:{s:4d}  DYN:{d:4d}  REG:{g:4d}")

    # Veto analysis
    for vlabel, vlist in [
        ("NEW-STRICT", strict_vetoes),
        ("NEW-DYNAMIC", dyn_vetoes),
        ("NEW-REGIME", regime_vetoes),
    ]:
        print(f"\n  --- {vlabel} vetoes / exceptions ---")
        print(f"  Total entries in veto log: {len(vlist)}")
        if vlist:
            blocks = [v for v in vlist if "EXCEPTION" not in v["reason"]]
            exc = [v for v in vlist if "EXCEPTION" in v["reason"]]
            reason_counts = Counter(v["reason"].split("(")[0].strip() for v in blocks)
            print(f"  Hard blocks: {len(blocks)} | Reduced-size allowed: {len(exc)}")
            for reason, cnt in reason_counts.most_common(8):
                pct_s = f"{cnt/len(blocks)*100:.0f}%" if blocks else "0%"
                print(f"    {reason:<40s}: {cnt:4d} ({pct_s})")

    # Per-regime breakdown for REGIME variant (actual labels from Trade)
    if regime["n"] > 0:
        print(f"\n  --- REGIME: entry distribution by regime label ---")
        regime_counts: Counter = Counter()
        regime_pnl: Dict[str, float] = {}
        regime_wins: Dict[str, int] = {}
        regime_size: Dict[str, List[float]] = {}
        for t in regime["trades"]:
            label = t.regime_label or "UNKNOWN"
            regime_counts[label] += 1
            regime_pnl[label] = regime_pnl.get(label, 0.0) + t.pnl_usd
            regime_size.setdefault(label, []).append(t.size_mult)
            if t.pnl_usd > 0:
                regime_wins[label] = regime_wins.get(label, 0) + 1
        for label in ["STRONG_TREND", "TREND", "NEUTRAL", "CHOP", "UNKNOWN"]:
            cnt = regime_counts.get(label, 0)
            if cnt:
                pnl = regime_pnl.get(label, 0.0)
                wr = regime_wins.get(label, 0) / cnt * 100
                avg_sz = sum(regime_size[label]) / len(regime_size[label])
                print(f"    {label:<16s}: {cnt:4d} trades  WR {wr:5.1f}%  "
                      f"avg size x{avg_sz:.2f}  PnL ${pnl:+8.2f}")

    # Worst/best 3 per variant
    for stat in cols:
        if stat.get("trades"):
            print(f"\n  --- {stat['label']} worst 3 ---")
            for t in sorted(stat["trades"], key=lambda t: t.pnl_usd)[:3]:
                print(f"    {t.coin:8s} {t.side:5s} RSI={t.rsi_at_entry:.0f} "
                      f"ext={t.ext_atr_at_entry:+.1f} sz×{t.size_mult:.1f}  "
                      f"${t.pnl_usd:+.2f}  {t.exit_reason}")

    print(f"\n  Caveats:")
    print(f"    - AI verdict substituted with deterministic heuristic")
    if cost_note:
        print(f"    - {cost_note}")
    else:
        print(f"    - Round-trip fee {ROUND_TRIP_FEE_BPS:.1f} bps, no slippage/funding")
    print(f"    - One position per coin; max_concurrent not enforced across coins")
    print(f"    - No cooldown, no compounding, no equity curve dynamics")
    print(f"    - REGIME: CHOP 0.5x size (A); TREND RSI 40-60 halved (B); "
          f"STRONG_TREND/TREND max_loss 0.8% vs CHOP/NEUTRAL 0.4% (C)")
    print(f"    - STRONG_TREND widens RSI to 95/5 and ext to 3.5 ATR")
    print(f"    - Past performance does NOT imply future results")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--equity", type=float, default=200.0)
    ap.add_argument("--max-loss", type=float, default=None,
                    help="Override DSL max_loss_pct (e.g. 1.0 for 1%%)")
    # H-7 cost model knobs.
    ap.add_argument("--entry-slip-bps", type=float, default=DEFAULT_ENTRY_SLIP_BPS,
                    help="Adverse entry slippage in bps (H-7; default %(default)s)")
    ap.add_argument("--exit-slip-bps", type=float, default=DEFAULT_EXIT_SLIP_BPS,
                    help="Adverse exit slippage in bps (H-7; default %(default)s)")
    ap.add_argument("--stop-delay-slip-bps", type=float, default=DEFAULT_STOP_DELAY_SLIP_BPS,
                    help="Extra adverse bps on max_loss stop-outs (default %(default)s)")
    ap.add_argument("--use-memory-slip", action="store_true",
                    help="Backfill per-coin exit slippage from memory.avg_exit_slip_bps")
    ap.add_argument("--use-memory-fee", action="store_true",
                    help="O-8: calibrate per-coin round-trip fee from "
                         "memory.avg_round_trip_fee_bps (actual exchange fee_usd; "
                         "falls back to the 5-bps default on thin samples)")
    ap.add_argument("--no-slippage", action="store_true",
                    help="Zero all slippage (restore the fee-only baseline)")
    args = ap.parse_args()

    from hermes_trader.agents.config_store import read_agent_config, cfg_get
    live = read_agent_config()
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    lev_ceiling = int(cfg_get("leverage", config=live))
    dsl = live.get("dsl_exit", {}) or {}
    max_loss = args.max_loss if args.max_loss is not None else float(cfg_get("dsl_exit.max_loss_pct", config=dsl))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl))

    # H-7 cost model (see constants above).
    if args.no_slippage:
        entry_slip = exit_slip = stop_delay_slip = 0.0
    else:
        entry_slip = float(args.entry_slip_bps)
        exit_slip = float(args.exit_slip_bps)
        stop_delay_slip = float(args.stop_delay_slip_bps)
    _mem = None
    if args.use_memory_slip and not args.no_slippage:
        try:
            from hermes_trader.agents.memory import memory as _mem
        except Exception as _e:  # best-effort: fall back to the static default
            print(f"  [warn] memory slip unavailable ({_e}); using default exit slip")
            _mem = None
    # O-8: measured round-trip fee source (independent of the slip toggle).
    _mem_fee = None
    if args.use_memory_fee:
        try:
            from hermes_trader.agents.memory import memory as _mem_fee
        except Exception as _e:  # best-effort: fall back to the static default
            print(f"  [warn] memory fee unavailable ({_e}); using default fee")
            _mem_fee = None
    if args.no_slippage:
        cost_note = (f"Round-trip fee {ROUND_TRIP_FEE_BPS:.1f} bps only "
                     f"(--no-slippage); no slippage/funding")
    else:
        cost_note = (f"Fee {ROUND_TRIP_FEE_BPS:.1f} bps RT + adverse slip "
                     f"entry {entry_slip:.1f}/exit {exit_slip:.1f} bps"
                     f"{' (per-coin via memory)' if _mem is not None else ''}"
                     f", +{stop_delay_slip:.1f} bps on max_loss stop-outs; no funding")

    bars_per_day = 24
    total_bars = args.days * bars_per_day + 150

    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    print(f"=== A/B/C backtest: OLD vs STRICT vs DYNAMIC vs REGIME ===")
    print(f"Period: {args.days} days | Universe: top-{args.coins} by volume | Equity: ${args.equity:.0f}")
    print(f"Fraction: {equity_fraction:.0%} | Lev ceiling: {lev_ceiling}x | DSL: {max_loss}%/{protect}%/{retrace}")
    print()

    old_trades: List[Trade] = []
    new_trades: List[Trade] = []
    dyn_trades: List[Trade] = []
    regime_trades: List[Trade] = []
    strict_vetoes: List[Dict[str, Any]] = []
    dyn_vetoes: List[Dict[str, Any]] = []
    regime_vetoes: List[Dict[str, Any]] = []

    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        # H-7: per-coin measured adverse exit slippage overrides the default
        # when enough live samples exist (memory returns 0.0 otherwise).
        coin_exit_slip = exit_slip
        if _mem is not None:
            try:
                _ms = float(_mem.avg_exit_slip_bps(coin))
                if _ms > 0.0:
                    coin_exit_slip = _ms
            except Exception:
                pass
        # O-8: per-coin measured round-trip fee (falls back to the default).
        coin_fee_bps = ROUND_TRIP_FEE_BPS
        if _mem_fee is not None:
            try:
                _mf = float(_mem_fee.avg_round_trip_fee_bps(coin))
                if _mf > 0.0:
                    coin_fee_bps = _mf
            except Exception:
                pass
        try:
            candles = fetch_hl_candles(coin, "1h", total_bars)
            if len(candles) < 150:
                print(f"  {coin:10s} skip ({len(candles)} bars)")
                continue
            ot = _simulate(
                coin, candles, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
                cfg=cfg, use_new_rules=False,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace,
                entry_slip_bps=entry_slip, exit_slip_bps=coin_exit_slip,
                stop_delay_slip_bps=stop_delay_slip, fee_bps=coin_fee_bps,
            )
            nt = _simulate(
                coin, candles, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
                cfg=cfg, use_new_rules=True,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace, veto_log=strict_vetoes,
                rsi_variant="strict",
                entry_slip_bps=entry_slip, exit_slip_bps=coin_exit_slip,
                stop_delay_slip_bps=stop_delay_slip, fee_bps=coin_fee_bps,
            )
            dt = _simulate(
                coin, candles, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
                cfg=cfg, use_new_rules=True,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace, veto_log=dyn_vetoes,
                rsi_variant="dynamic",
                entry_slip_bps=entry_slip, exit_slip_bps=coin_exit_slip,
                stop_delay_slip_bps=stop_delay_slip, fee_bps=coin_fee_bps,
            )
            gt = _simulate(
                coin, candles, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
                cfg=cfg, use_new_rules=True,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace, veto_log=regime_vetoes,
                rsi_variant="regime",
                entry_slip_bps=entry_slip, exit_slip_bps=coin_exit_slip,
                stop_delay_slip_bps=stop_delay_slip, fee_bps=coin_fee_bps,
            )
            op = sum(t.pnl_usd for t in ot)
            np_ = sum(t.pnl_usd for t in nt)
            dp = sum(t.pnl_usd for t in dt)
            gp = sum(t.pnl_usd for t in gt)
            ow = sum(1 for t in ot if t.pnl_usd > 0)
            nw = sum(1 for t in nt if t.pnl_usd > 0)
            dw = sum(1 for t in dt if t.pnl_usd > 0)
            gw = sum(1 for t in gt if t.pnl_usd > 0)
            print(f"  {coin:10s}  OLD:{len(ot):3d}tr/{ow:3d}W/${op:+7.0f}  "
                  f"STR:{len(nt):3d}tr/{nw:3d}W/${np_:+7.0f}  "
                  f"DYN:{len(dt):3d}tr/{dw:3d}W/${dp:+7.0f}  "
                  f"REG:{len(gt):3d}tr/{gw:3d}W/${gp:+7.0f}")
            old_trades.extend(ot)
            new_trades.extend(nt)
            dyn_trades.extend(dt)
            regime_trades.extend(gt)
        except Exception as e:
            import traceback
            print(f"  {coin:10s} error: {e}")
            traceback.print_exc()

    old_stats = _stats(old_trades, args.equity, args.days, "OLD")
    new_stats = _stats(new_trades, args.equity, args.days, "NEW-STRICT")
    dyn_stats = _stats(dyn_trades, args.equity, args.days, "NEW-DYNAMIC")
    regime_stats = _stats(regime_trades, args.equity, args.days, "NEW-REGIME")
    _print_report(
        old_stats, new_stats, dyn_stats, regime_stats,
        strict_vetoes, dyn_vetoes, regime_vetoes,
        args.days, cost_note=cost_note,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
