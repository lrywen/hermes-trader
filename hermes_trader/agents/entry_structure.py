"""Structural entry overlays: portfolio vol-target, regime breakout veto,
cross-sectional directional momentum.

PURE / FAIL-SAFE HELPERS. The percentile math and multipliers contain no
network or config IO (callers supply candles/positions), so they are trivially
testable and carry zero live impact until wired in.

These implement the P1+P2 structural layer agreed for the momentum system:

1. ``vol_target_multiplier`` — a PORTFOLIO-level scale in (0,1]. ATR equal-risk
   sizing (sizing.py) sets each trade from its own stop, but nothing currently
   bounds the book's aggregate volatility. This maps the trailing realised
   portfolio sigma to a multiplier (Man AHL / M-9 style): sigma at or above
   target → 1.0; above target → scale DOWN, capped so it can only trim the tail.
   M-9 (180d replay) showed this cuts max drawdown ~-30%→-15% without raising
   return; it is tail control, not alpha.

2. ``breakout_regime_veto`` — a hard switch disabling BREAKOUT-chase entries in
   a ranging/chop regime. The existing market_regime gate already RAISES the
   confidence/score bar in chop, but there is no on/off switch. In a confirmed
   range, breakouts are mean-reverting (the move fades), so chasing them is the
   exact losing pattern observed. Returns a block reason in chop/neutral for a
   breakout trigger; non-breakout triggers (e.g. pullback) are unaffected.

3. ``cross_sectional_momentum`` — a directional (signed), per-coin relative-
   strength score across the universe, replacing the direction-AGNOSTIC
   activity rank used for coin selection. ``select_top_n`` keeps only the
   strongest longs (and, if ever enabled, weakest shorts) so the book
   concentrates in genuine leaders rather than every coincident breakout.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


# ── 1. Portfolio volatility target ─────────────────────────────────────────

def vol_target_multiplier(
    *,
    sigma: float,
    target_sigma: float,
    cap: float = 2.0,
    floor: float = 0.0,
) -> float:
    """Scale a candidate notional by target_sigma / realised sigma.

    Returns a multiplier in [floor, min(cap, ...)]. Sigma at target → 1.0;
    above target → trim; below target allowed up to ``cap`` (default keeps it
    from LEVERING UP into low vol — set cap>1 only deliberately). Non-finite /
    non-positive inputs fail safe to 1.0 (no scaling).
    """
    try:
        if (not math.isfinite(sigma)) or sigma <= 0.0:
            return 1.0
        if (not math.isfinite(target_sigma)) or target_sigma <= 0.0:
            return 1.0
        mult = target_sigma / sigma
        mult = max(0.0, min(float(cap), mult))
        if floor:
            mult = max(float(floor), mult)
        return mult
    except (TypeError, ValueError):
        return 1.0


def realised_sigma(log_returns: Sequence[float]) -> Optional[float]:
    """Annualised volatility from a window of log returns (population std).

    Caller controls the bar interval and annualisation window by supplying the
    appropriately-scaled returns. Returns None when fewer than 2 points."""
    try:
        r = [float(x) for x in log_returns if math.isfinite(float(x))]
        n = len(r)
        if n < 2:
            return None
        mean = sum(r) / n
        var = sum((x - mean) ** 2 for x in r) / n
        return math.sqrt(var)
    except (TypeError, ValueError):
        return None


# ── 2. Regime breakout veto ────────────────────────────────────────────────

def breakout_regime_veto(
    *,
    regime: str,
    trigger: str,
    block_in: Sequence[str] = ("chop", "neutral"),
) -> Optional[str]:
    """Block a BREAKOUT-chase entry in a non-trending regime.

    ``regime`` is the resolved macro/decision regime ('up'/'down'/'neutral'/
    'chop'); ``trigger`` describes what fired ('breakout'/'recent_breakout'/
    'burst'/'pullback'/'daily_mover'). Only breakout-family triggers are
    vetoed and only while the regime is in ``block_in``. Returns a human reason
    when blocked, else None. Longs need 'up', shorts (if enabled) need 'down'.
    """
    try:
        r = str(regime or "neutral").lower()
        t = str(trigger or "").lower()
    except (TypeError, ValueError):
        return None
    breakout_triggers = ("breakout", "recent_breakout")
    if t not in breakout_triggers:
        return None
    if r in {str(x).lower() for x in block_in}:
        return (f"breakout entry vetoed in {r} regime — range breakouts fade, "
                f"no chase (use pullback or wait for trend confirmation)")
    return None


# ── 3. Cross-sectional directional momentum ────────────────────────────────

def directional_momentum_score(
    *,
    px_now: float,
    px_past: float,
) -> Optional[float]:
    """Signed relative return over the lookback (positive = up). None if bad."""
    try:
        now, past = float(px_now), float(px_past)
        if past <= 0.0 or not (math.isfinite(now) and math.isfinite(past)):
            return None
        return (now - past) / past
    except (TypeError, ValueError):
        return None


def rank_by_momentum(
    coins: Sequence[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Sort (coin, signed_momentum) pairs strongest-first. Drops invalid."""
    valid = [(str(c), float(s)) for c, s in coins
             if math.isfinite(float(s))]
    return sorted(valid, key=lambda x: x[1], reverse=True)


def select_top_n(
    ranked: Sequence[tuple[str, float]],
    *,
    n: int,
    side: str = "long",
    require_positive: bool = True,
) -> list[str]:
    """Top-N coin symbols for the side from a strongest-first momentum ranking.

    For longs keeps the top of the list and, when ``require_positive``, only
    positive momentum. Shorts (if ever enabled) take the WEAKEST from the tail.
    """
    n = max(0, int(n))
    if n == 0:
        return []
    pool = list(ranked)
    if side == "short":
        pool = list(reversed(pool))
        if require_positive:
            pool = [x for x in pool if x[1] < 0.0]
    elif require_positive:
        pool = [x for x in pool if x[1] > 0.0]
    return [c for c, _ in pool[:n]]
