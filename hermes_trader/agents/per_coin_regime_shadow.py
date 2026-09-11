"""Per-coin 4h direction-confirmity shadow probe (坑1 quick fix).

The market_regime gate classifies every crypto perp by the BTC proxy
(CRYPTO_PROXY="BTC"). When BTC trends up a long in an alt is "aligned" and
free-passes — even if the alt's OWN 4h is rolling over (the ZEC case:
BTC=up/score 0.61 -> via=aligned, ZEC MFE 0.016% -> -0.83% stop).

This module is a SHADOW-ONLY quick probe: when the macro call is "aligned" but
the coin's OWN 4h EMA points against the trade, it records what a soft
"weak_aligned" demotion WOULD do, without changing the live gate verdict. The
own-4h readings (ema21_4h / close4h / adx4h / atr4h) are already carried on
the research `analysis` dict (research.py), so this adds no network fetch.

Counter-factual policy being measured (mirrors the market_regime gate's
weak_aligned demotion):
    macro aligned AND own 4h EMA against the side AND own adx4h confirms a
    real own-trend (>= require_own_adx)  -> would_demote=True
The demotion would route the trade to the counter-trend bar (conf / composite)
instead of the aligned free-pass.

Pure helpers are offline-testable; record_* only appends a JSONL line and never
raises into the gate path. Flip to enforce in a later change after the shadow
EV is positive.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SHADOW_FILE_ENV = "HERMES_PER_COIN_REGIME_SHADOW_FILE"
_SHADOW_FILE_DEFAULT = "~/.hermes-trading/per_coin_regime_shadow.jsonl"


def _shadow_file(blk: dict[str, Any]) -> str:
    return str(blk.get("shadow_log_path") or "").strip() or os.environ.get(
        _SHADOW_FILE_ENV, os.path.expanduser(_SHADOW_FILE_DEFAULT))


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and f != float("inf") else None
    except (TypeError, ValueError):
        return None


def own_4h_divergence(side: str, analysis: Optional[dict[str, Any]],
                      require_own_adx: float = 20.0) -> dict[str, Any]:
    """Pure: decide whether the coin's OWN 4h direction contradicts a macro-
    aligned trade. Returns a dict with the own-4h readings and flags.

    Rules:
      * long  is "own-down" when close4h < ema21_4h (own fast EMA rolling over)
      * short is "own-up"   when close4h > ema21_4h
      * require own adx4h >= require_own_adx to treat the own-direction as a
        real trend (a weak cross in a range is not evidence to demote).
    No data / NaN -> confirmed=False (fail OPEN: never manufacture a demotion).
    """
    empty = {"own_direction": "unknown", "own_down": False, "own_up": False,
             "adx_confirms": False, "would_demote": False,
             "close4h": None, "ema21_4h": None, "adx4h": None,
             "own_gap_pct": None}
    if not isinstance(analysis, dict):
        return empty
    close4h = _f(analysis.get("close4h"))
    ema21 = _f(analysis.get("ema21_4h"))
    adx4h = _f(analysis.get("adx4h"))
    if close4h is None or ema21 is None or ema21 <= 0:
        return empty

    own_down = close4h < ema21
    own_up = close4h > ema21
    gap_pct = (close4h - ema21) / ema21 * 100.0
    adx_confirms = (adx4h is not None and adx4h >= require_own_adx)
    s = str(side or "").lower()
    against = (s == "long" and own_down) or (s == "short" and own_up)
    return {
        "own_direction": "down" if own_down else "up" if own_up else "flat",
        "own_down": own_down,
        "own_up": own_up,
        "adx_confirms": bool(adx_confirms),
        # Only a macro-ALIGNED trade is demoted (caller checks macro alignment);
        # here we report whether own-4h disagrees with enough conviction.
        "would_demote": bool(against and adx_confirms),
        "close4h": round(close4h, 8),
        "ema21_4h": round(ema21, 8),
        "adx4h": (round(adx4h, 3) if adx4h is not None else None),
        "own_gap_pct": round(gap_pct, 4),
    }


def _macro_aligned(regime: str, side: str) -> bool:
    s = str(side or "").lower()
    return (regime == "up" and s == "long") or (regime == "down" and s == "short")


def _own_aligned(own_regime: str, side: str) -> bool:
    s = str(side or "").lower()
    return (own_regime == "up" and s == "long") or (own_regime == "down" and s == "short")


def _own_against(own_regime: str, side: str) -> bool:
    s = str(side or "").lower()
    return (own_regime == "down" and s == "long") or (own_regime == "up" and s == "short")


def quadrant_tier(*, macro_regime: str, macro_score: Optional[float],
                  own_regime: str, own_score: Optional[float], side: str,
                  strong_score: float, mid_score: float) -> dict[str, Any]:
    """Pure macro×own quadrant + 3-tier strength classification (shadow).

    Tiers (the structural replacement for a single global min_trend_score):
      strong        macro aligned AND own 1h aligned AND own score>=strong_score
      mid           macro aligned AND own not against (neutral/chop/aligned-weak)
      weak_review   macro aligned BUT own 1h points AGAINST the side
                    (the ZEC quadrant: BTC up, alt rolling over)
    Counter/neutral macro regimes are out of scope for this overlay and return
    tier="n/a" (the existing gate already handles them).
    """
    if not _macro_aligned(macro_regime, side):
        return {"tier": "n/a", "own_aligned": False, "own_against": False,
                "would": "pass"}
    aligned = _own_aligned(own_regime, side)
    against = _own_against(own_regime, side)
    if against:
        tier, would = "weak_review", "counter_review"
    elif aligned and own_score is not None and own_score >= strong_score:
        tier, would = "strong", "free_pass"
    else:
        tier, would = "mid", "light_bar"
    return {"tier": tier, "own_aligned": aligned, "own_against": against,
            "would": would, "mid_score": mid_score, "strong_score": strong_score}


def record_per_coin_regime_shadow(*, coin: str, side: str,
                                  confidence: float, composite_score: float,
                                  market_regime_result: dict[str, Any],
                                  analysis: Optional[dict[str, Any]],
                                  config: dict[str, Any],
                                  trace_id: str = "",
                                  extra_detail: Optional[dict[str, Any]] = None,
                                  ) -> None:
    """Append a per-coin direction shadow record.

    Shadow-only: gated by per_coin_regime_shadow.shadow_mode (default False so
    the probe is inert until explicitly enabled). Never raises.

    Coverage (probe-idle fix): historically this returned early unless the
    trade was macro-ALIGNED, so during long non-aligned stretches the file
    stayed empty and the overlay could never accumulate a reconciliation
    sample. With record_all_quadrants (default True) it now records EVERY
    evaluated crypto candidate with its macro×own quadrant; macro-ALIGNED rows
    carry the actionable would=demote/pass label, while non-aligned rows are
    tagged would="n/a_non_aligned" (the existing gate already owns those).
    Set record_all_quadrants=false to restore the aligned-only behaviour.
    """
    try:
        blk = (config or {}).get("per_coin_regime_shadow") or {}
        if not bool(blk.get("shadow_mode", False)):
            return
        if not isinstance(market_regime_result, dict):
            return
        regime = str(market_regime_result.get("regime") or "")
        aligned = _macro_aligned(regime, side)
        # Default True: keep recording across non-aligned stretches so the
        # probe no longer idles. An explicit false restores aligned-only.
        record_all = bool(blk.get("record_all_quadrants", True))
        if not aligned and not record_all:
            return
        require_adx = _f(blk.get("require_own_adx"))
        div = own_4h_divergence(side, analysis,
                                require_adx if require_adx is not None else 20.0)
        macro_score = _f(market_regime_result.get("trend_score"))

        # 坑1 root-fix + 坑2 structural tier: fetch the coin's OWN 1h macro
        # (TTL cached, only when the probe is enabled) and compute the
        # macro×own quadrant / 3-tier strength bucket. Shadow-only.
        own_regime, own_score = "neutral", None
        try:
            from hermes_trader.agents.market_regime import (
                detect_own_regime_with_score)
            own_regime, own_score = detect_own_regime_with_score(coin)
        except Exception:
            own_regime, own_score = "neutral", None
        strong_score = _f(blk.get("strong_own_score"))
        mid_score = _f(blk.get("mid_own_score"))
        quad = quadrant_tier(
            macro_regime=regime, macro_score=macro_score,
            own_regime=own_regime, own_score=own_score, side=side,
            strong_score=strong_score if strong_score is not None else 0.65,
            mid_score=mid_score if mid_score is not None else 0.55)

        would = ("demote_to_weak_aligned" if div["would_demote"] else "pass") \
            if aligned else "n/a_non_aligned"
        rec = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trace_id": trace_id or "",
            "rule": "per_coin_regime",
            "coin": coin,
            "side": str(side or "").lower(),
            "macro_aligned": aligned,
            "would": would,
            "detail": {
                "macro_regime": regime,
                "macro_trend_score": (round(macro_score, 3)
                                      if macro_score is not None else None),
                "macro_via": market_regime_result.get("via"),
                "composite_score": _f(composite_score),
                "confidence": _f(confidence),
                "own_1h_regime": own_regime,
                "own_1h_score": (round(own_score, 3)
                                 if own_score is not None else None),
                "quadrant_tier": quad["tier"],
                "tier_would": quad["would"],
                **div,
                **(extra_detail or {}),
            },
            "outcome": None,    # filled by offline reconciliation
            "exit_px": None,
            "pnl_usd": None,
        }
        from hermes_trader.shadow_log import append_jsonl
        path = _shadow_file(blk)
        if append_jsonl(path, rec, stream="per_coin_regime"):
            # Macro-aligned rows are the actionable failure mode → INFO. The
            # expanded non-aligned coverage only feeds reconciliation, so keep
            # it at DEBUG so a busy tape doesn't flood the log.
            _log = logger.info if aligned else logger.debug
            _log(
                "[risk_gates] per-coin-regime SHADOW for %s: would=%s tier=%s "
                "(macro=%s/%s own1h=%s/%s own4h=%s gap=%s%%) -> %s",
                coin, rec["would"], quad["tier"], regime, macro_score,
                own_regime, own_score, div["own_direction"],
                div["own_gap_pct"], path)
    except Exception as e:  # never break the gate path
        logger.debug("[per_coin_regime_shadow] record failed: %s", e)
