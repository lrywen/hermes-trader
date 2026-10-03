"""Limit-order-with-TTL execution plan (strategy-paradigm P2).

Hermes entries are historically immediate-or-cancel taker orders, paying the
full spread plus slippage. The ``maker_execution`` block can post a passive
limit order but its ``resting_lifecycle_ready`` guard stays fail-closed until a
lifecycle exists for: polling the resting order, cancelling it on timeout, and
falling back to a taker order so the signal is never lost.

This module is the pure, side-effect-free core of that lifecycle:

  * ``limit_price``      — where the passive (or spread-crossing) limit rests
  * ``ttl_expired``      — has the resting window elapsed
  * ``advance``          — the deterministic resting-order state machine
                           (resting -> filled / cancelled-then-fallback)

It performs NO venue writes and reads no clock: callers pass ``now`` and the
order-status booleans. Wiring the real primitives (place / find_open_order /
cancel / taker fallback) is a separate, LIVE-only step gated by
``maker_execution.resting_lifecycle_ready`` and the B-13 gate. Keeping the
decision logic pure makes the TTL/fallback contract unit-testable now, while
the system is still in SHADOW.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

# State machine states.
RestingState = Literal["resting", "filled", "expired_open", "fallback_taker"]


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def limit_price(mid: float, is_buy: bool, offset_bps: float,
                *, cross: bool = False) -> float:
    """Return the limit price for a passive entry.

    ``offset_bps`` is in basis points relative to mid. For a passive post-only
    order the buy limit sits BELOW mid and the sell limit above; when ``cross``
    is set (aggressive limit that still improves on a taker sweep) the offsets
    invert to cross inside the spread.
    """
    off = max(0.0, _f(offset_bps)) / 10_000.0
    if not cross:
        return mid * ((1.0 - off) if is_buy else (1.0 + off))
    return mid * ((1.0 + off) if is_buy else (1.0 - off))


def expected_maker_improvement_bps(spread_bps: float, offset_bps: float) -> float:
    """Estimated saving vs a taker sweep, in bps, if the maker fills.

    A taker pays half the spread; the maker rests ``offset_bps`` inside it, so
    the improvement is half-spread minus the passive offset. Floored at 0: a
    limit that would not beat taker should not be used.
    """
    return max(0.0, 0.5 * _f(spread_bps) - max(0.0, _f(offset_bps)))


def ttl_expired(submitted_at: float, now: float, ttl_s: float) -> bool:
    """True when the resting window has elapsed. Invalid TTL -> never expire."""
    ttl = _f(ttl_s, -1.0)
    if ttl < 0:
        return False
    return (now - _f(submitted_at)) >= ttl


def should_attempt_maker(
    *, spread_bps: float, offset_bps: float, min_improvement_bps: float) -> bool:
    """Gate: only post the maker when it is expected to beat taker enough."""
    return expected_maker_improvement_bps(spread_bps, offset_bps) >= _f(
        min_improvement_bps)


def advance(
    state: Mapping[str, Any],
    now: float,
    *,
    is_filled: bool,
    still_open: bool,
) -> tuple[RestingState, dict[str, Any]]:
    """Advance one resting-order lifecycle tick. Pure; returns (new_state, plan).

    Expected input state keys: ``status``, ``submitted_at``, ``ttl_s``.
    Transitions:
      * filled at any time          -> "filled" (no further action)
      * resting & within TTL        -> "resting" (keep polling)
      * resting & TTL expired, open -> "expired_open" (CANCEL then taker)
      * resting & TTL expired, gone -> "fallback_taker" (order not open: taker)
      * expired_open                -> "fallback_taker" (after cancel issued)
      * filled / fallback_taker     -> terminal (unchanged)

    The ``plan`` dict carries the side effect the caller should perform
    (``cancel`` / ``place_taker`` / ``wait``), keeping this function free of
    venue writes.
    """
    status = str(state.get("status", "resting"))
    submitted_at = _f(state.get("submitted_at"))
    ttl_s = _f(state.get("ttl_s"))

    if status == "filled":
        return "filled", {"action": "none"}
    if status == "fallback_taker":
        return "fallback_taker", {"action": "none"}

    if is_filled:
        return "filled", {"action": "none"}

    expired = ttl_expired(submitted_at, now, ttl_s)

    if status == "expired_open":
        # Cancel was requested last tick; whether or not it is still open we now
        # go taker (a residual fill is reconciled by the caller before sizing).
        return "fallback_taker", {"action": "place_taker"}

    if not expired:
        return "resting", {"action": "wait"}

    if still_open:
        return "expired_open", {"action": "cancel"}
    return "fallback_taker", {"action": "place_taker"}
