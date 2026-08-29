"""Agent trigger configuration: trigger weights, thresholds, and scan settings."""

from __future__ import annotations

import logging
from typing import Any

from hermes_trader.agents.config_store import cfg_get

logger = logging.getLogger(__name__)


TRIGGER_CONFIG: dict[str, Any] = {
    "weights": {
        # RE-WEIGHTED 2026-06-02 to MEASURED MARGINAL LIFT (fired vs not-fired ROE,
        # n=497 trades). Prior weights were inverted: the 1h slow-burn signals carried
        # the heaviest weight (0.60/0.55/0.40) but had ~0/negative lift, while
        # trendStrength (the BEST signal, +2.08% lift) was only 0.10. Weights now
        # track lift; net-negative triggers (trendFlip1h -2.10%, rangeCompression
        # -3.08%) are ZEROED out of scoring.
        "trendStrength": 0.55,    # lift +2.08% (was 0.10) — strongest edge
        "pctMoveSpike": 0.40,     # lift +1.49%
        "breakout": 0.30,         # lift +1.29%
        "volumeSpike": 0.25,      # lift +1.05%
        "momentumBurst": 0.20,    # lift +0.77% (n=9, kept modest)
        "volumeBuildup1h": 0.15,  # lift +0.41% (was 0.60 — overweighted)
        "higherLows1h": 0.0,      # lift -0.51% — removed
        "trendFlip1h": 0.0,       # lift -2.10% — removed (net loser)
        "rangeCompression": 0.0,  # lift -3.08% — removed (worst)
        # Symmetric directional SURFACING triggers — weight 0 so they don't touch
        # the composite denominator (no gate recalibration). They surface trending
        # coins via the bypass in perception, not via score. Removes the long-bias
        # in surfacing so down-movers reach research and can be shorted.
        "uptrendMomentum": 0.0,
        "downtrendMomentum": 0.0,
        "dailyMover": 0.0,
    },
    "thresholds": {
        "sigmaThreshold": 2.0,
        "trendMomentumLookback": 72,  # 5m bars (~6h) for sustained up/down trend surfacing
        "trendMomentumPct": 5.0,      # min |%| move over ~6h to surface (5%: 3.0 over-surfaced — 22 triggers/scan, ~4.5x AI cost, flooded longs)
        "breakoutLookback": 48,
        "breakoutMinRvol": 1.5,       # RVOL threshold: close break only fires when current vol >= this × prior avg
        "breakoutRvolWindow": 20,     # prior-bar window for the RVOL average
        "breakoutAtrScoreMult": 3.0,  # ATR-normalized score multiplier: min(10, distance/ATR * mult)
        "breakoutConfirmBars": 2,     # consecutive closes beyond the edge required to fire (rejects 1-bar fakeouts)
        "bbLength": 20,
        "bbStdDev": 2,
        "adxPeriod": 14,
        "momentumLookback": 2,   # 5m bars in the momentum_burst window (-> 10 min)
        "momentumPct": 4.0,      # min % move over that window to fire momentum_burst
        "volBuildupRatio": 2.5,  # 4h vs prior 20h avg, on 1h candles
        "trendFlipBars": 3,      # EMA8/21 cross within last N 1h bars
        "higherLowsRequired": 4, # of last 6 1h bars
    },
    "scan": {
        "minCompositeScore": 54,  # recalibrated for new weights: P230 zeroed 3 triggers -> denom 2.85->1.85 -> scores ~1.54x. 54 preserves the old-35 selectivity (35*1.54). Without this the gate silently loosened.
        "candleInterval": "5m",
        "candleCount": 100,
        "cacheTtlMs": 50_000,
        # 1h candles don't change mid-hour; cache 10min so we only refetch
        # every 10 scans, keeping the per-cycle weight budget intact.
        "cacheTtlMs1h": 600_000,
        # Score CLOSED bars only: when the last 5m/1h bar is still forming,
        # drop it and evaluate the last completed bar. Fixes the missed-surge
        # bug where a strong close (e.g. BTC 12:40) was scored mid-formation at
        # ~40% progress (composite 36 < gate 54), then slid to [-2] unevaluated
        # when the next bar opened. Intra-bar signals now arrive at most one
        # bar (~5min) late, but are deterministic.
        "evaluateClosedBarsOnly": True,
        # Within this many ms after a bar closes, bypass the candle cache to
        # guarantee we read the just-completed bar instead of a pre-close
        # snapshot still within cacheTtlMs.
        "postCloseForceRefreshMs": 15_000,
    },
}


def get_config() -> dict[str, Any]:
    """Return the default trigger configuration."""
    return TRIGGER_CONFIG


# ── R13-B8: canonical hot-path resolution for weights / thresholds ──────────
# These literals mirror TRIGGER_CONFIG verbatim and stay as the fallback
# symbols (backtest scripts import get_config() directly). The CANONICAL
# `trigger_weights` / `trigger_thresholds` blocks are the single source of
# truth at runtime; leaf names there are snake_case, while consumers
# (triggers.composite_score indexes by trigger name; _scan_single_market
# reads thresholds["camelKey"]) expect the historical camelCase keys, so the
# helpers below map canonical leaves back to the runtime key names.

# snake_case canonical leaf -> camelCase runtime key (weights).
_TRIGGER_WEIGHTS_KEYMAP: dict[str, str] = {
    "trend_strength": "trendStrength",
    "pct_move_spike": "pctMoveSpike",
    "breakout": "breakout",
    "volume_spike": "volumeSpike",
    "momentum_burst": "momentumBurst",
    "volume_buildup_1h": "volumeBuildup1h",
    "higher_lows_1h": "higherLows1h",
    "trend_flip_1h": "trendFlip1h",
    "range_compression": "rangeCompression",
    "uptrend_momentum": "uptrendMomentum",
    "downtrend_momentum": "downtrendMomentum",
    "daily_mover": "dailyMover",
}

# snake_case canonical leaf -> (camelCase runtime key, is_int) (thresholds).
_TRIGGER_THRESHOLDS_KEYMAP: dict[str, tuple[str, bool]] = {
    "sigma_threshold": ("sigmaThreshold", False),
    "trend_momentum_lookback": ("trendMomentumLookback", True),
    "trend_momentum_pct": ("trendMomentumPct", False),
    "breakout_lookback": ("breakoutLookback", True),
    "breakout_min_rvol": ("breakoutMinRvol", False),
    "breakout_rvol_window": ("breakoutRvolWindow", True),
    "breakout_atr_score_mult": ("breakoutAtrScoreMult", False),
    "breakout_confirm_bars": ("breakoutConfirmBars", True),
    "bb_length": ("bbLength", True),
    "bb_std_dev": ("bbStdDev", True),
    "adx_period": ("adxPeriod", True),
    "momentum_lookback": ("momentumLookback", True),
    "momentum_pct": ("momentumPct", False),
    "vol_buildup_ratio": ("volBuildupRatio", False),
    "trend_flip_bars": ("trendFlipBars", True),
    "higher_lows_required": ("higherLowsRequired", True),
}


def trigger_weights_params(*, config: dict[str, Any] | None = None) -> dict[str, float]:
    """Resolve the trigger composite-score weights (camelCase runtime keys).

    Returns the live ``trigger_weights`` canonical block (env
    ``HERMES_CFG_TRIGGER_WEIGHTS__*`` overrides included) with the
    TRIGGER_CONFIG literals as fallback. Guards: every weight must be a
    finite number >= 0 — six weights are intentionally 0.0 (net-negative /
    surfacing-only triggers), so negatives are rejected but zero is legal.
    Any read/coerce failure returns a fresh copy of the literals; the scan
    hot path must never break on a bad config.
    """
    p = {camel: TRIGGER_CONFIG["weights"][camel] for camel in _TRIGGER_WEIGHTS_KEYMAP.values()}
    try:
        for leaf, camel in _TRIGGER_WEIGHTS_KEYMAP.items():
            v = cfg_get(f"trigger_weights.{leaf}", config=config)
            if v is None:
                continue
            fv = float(v)
            if fv >= 0.0:
                p[camel] = fv
    except Exception as e:  # never let config break the scan path
        logger.debug(f"[config] trigger weights read failed: {e}")
        return {camel: TRIGGER_CONFIG["weights"][camel] for camel in _TRIGGER_WEIGHTS_KEYMAP.values()}
    return p


def trigger_thresholds_params(*, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the trigger thresholds (camelCase runtime keys).

    Returns the live ``trigger_thresholds`` canonical block (env
    ``HERMES_CFG_TRIGGER_THRESHOLDS__*`` overrides included) with the
    TRIGGER_CONFIG literals as fallback. Guards: float thresholds must be
    > 0, int thresholds must be >= 1 — malformed leaves keep the literal.
    Any read/coerce failure returns a fresh copy of the literals.
    """
    p = {spec[0]: TRIGGER_CONFIG["thresholds"][spec[0]]
         for spec in _TRIGGER_THRESHOLDS_KEYMAP.values()}
    try:
        for leaf, (camel, is_int) in _TRIGGER_THRESHOLDS_KEYMAP.items():
            v = cfg_get(f"trigger_thresholds.{leaf}", config=config)
            if v is None:
                continue
            if is_int:
                iv = int(v)
                if iv >= 1:
                    p[camel] = iv
            else:
                fv = float(v)
                if fv > 0.0:
                    p[camel] = fv
    except Exception as e:  # never let config break the scan path
        logger.debug(f"[config] trigger thresholds read failed: {e}")
        return {spec[0]: TRIGGER_CONFIG["thresholds"][spec[0]]
                for spec in _TRIGGER_THRESHOLDS_KEYMAP.values()}
    return p
