"""Direction-aware entry scoring (ADR-0012).

The production composite (``indicators.triggers.composite_score``) is
**direction-blind**: it weights every fired trigger by name only, with no
``side`` dimension. A SHORT candidate that fires a bullish trigger
(uptrendMomentum / higherLows1h / bullishReversalCandle) gets scored UP by
that bullish evidence. Combined with ``downtrendMomentum`` weight 0, shorts
are effectively ranked by bullish strength — the root cause documented in
ADR-0012 (B1).

This module adds a pure, side-aware scorer plus a directional freshness
verdict. Nothing here is wired into the live entry path in this change:

* ``side_aligned_score`` defaults to ``mode="legacy"`` and reproduces
  ``composite_score`` bit-for-bit (NaN/inf fail-closed included), so callers
  can A/B the two without behaviour change.
* ``mode="side_aware"`` counts only triggers aligned with the requested side
  (plus neutral ones), so bearish evidence can no longer inflate a long and
  vice versa.
* ``freshness_verdict`` defaults to ``mode="shadow"`` and always returns
  SHADOW (record-only); only ``enforce`` can return BLOCK, and enforce is not
  wired in this change.

Pure functions: no network, clock, or config IO. Safe on empty/short inputs.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

# Trigger-name direction classification. Mirrors the hits assembled by
# triggers.build_hits. Names not listed here are treated as NEUTRAL (count for
# both sides) — deliberately conservative: breakout/momentumBurst hits carry
# no direction field on the hit dict itself, so we never invent a side for them.
LONG_ONLY_TRIGGERS = frozenset({
    "uptrendMomentum",
    "higherLows1h",
    "bullishReversalCandle",
})
SHORT_ONLY_TRIGGERS = frozenset({
    "downtrendMomentum",
    "lowerHighs1h",
    "bearishReversalCandle",
})

VALID_SIDES = ("long", "short")
VALID_SCORE_MODES = ("legacy", "side_aware")
VALID_FRESHNESS_MODES = ("shadow", "enforce")


def trigger_alignment(name: str, side: str) -> int:
    """Return +1 if trigger ``name`` supports ``side``, -1 if it opposes it.

    Neutral triggers return +1 for both sides. A trigger name absent from the
    classification tables is neutral by construction.
    """
    if side not in VALID_SIDES:
        # Unknown side: never silently score; treat as opposing so the caller
        # notices rather than getting a phantom long-default (cf. executor
        # side default-to-long hazard noted in ADR-0011).
        return 0
    if name in LONG_ONLY_TRIGGERS:
        return 1 if side == "long" else -1
    if name in SHORT_ONLY_TRIGGERS:
        return 1 if side == "short" else -1
    return 1


def directional_weights_view(side: str, weights: Mapping[str, float]) -> dict[str, float]:
    """Expose the effective weight each trigger contributes for ``side``.

    Opposing triggers report 0; aligned and neutral triggers report their full
    weight. Pure view — never mutates ``weights``.
    """
    return {
        name: (float(w) if trigger_alignment(name, side) == 1 else 0.0)
        for name, w in weights.items()
    }


def side_aligned_score(
    side: str,
    hits: list[Mapping[str, Any]],
    weights: Mapping[str, float],
    mode: str = "legacy",
) -> float:
    """Direction-aware weighted composite in [0, 100].

    ``mode="legacy"`` reproduces ``triggers.composite_score`` exactly:

        fired = [h for h in hits if h.get("fired")]
        raw = sum(score * weight) / sum(ALL weights) * 10
        clamp; non-finite -> 0.0 (fail-closed)

    ``mode="side_aware"`` only sums fired triggers aligned with ``side`` (plus
    neutral ones). The denominator stays the total configured weight — NOT the
    side-filtered sum — so a side can never self-normalise to 100 off a single
    aligned trigger; bullish and bearish evidence compete on the same scale.
    """
    if mode not in VALID_SCORE_MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {VALID_SCORE_MODES}")

    fired = [h for h in hits if h.get("fired")]
    if not fired:
        return 0

    total_weight = sum(weights.values()) or 1

    def _contribution(hit: Mapping[str, Any]) -> float:
        score = hit.get("score", 0) or 0
        weight = float(weights.get(hit.get("name"), 0))
        if mode == "legacy":
            return float(score) * weight
        # side_aware: opposing evidence contributes nothing.
        if trigger_alignment(str(hit.get("name")), side) < 0:
            return 0.0
        return float(score) * weight

    weighted_sum = sum(_contribution(h) for h in fired)
    raw = (weighted_sum / total_weight) * 10
    # Match composite_score fail-closed: min(100, nan) == 100 under a plain
    # clamp, so an unfinite aggregate must collapse to 0, not the ceiling.
    if not math.isfinite(raw):
        return 0.0
    return max(0.0, min(100.0, raw))


# ── Directional freshness verdict (ADR-0012 §2.3) ─────────────────────────
# Production evidence (decision_shadow_outcomes, 1h / 9.26bp, direction-
# adjusted, day-block): long+fresh -73.8bp (chasing a fresh impulse = buying
# the end of the move); short+fresh ~+34bp (selling into the rip = contrarian
# fade). One rule cannot serve both directions.
FRESHNESS_ALLOW = "allow"
FRESHNESS_BLOCK = "block"
FRESHNESS_SHADOW = "shadow"


def freshness_verdict(
    side: str,
    row: Mapping[str, Any],
    mode: str = "shadow",
    *,
    fresh_key: str = "fresh_impulse",
) -> dict[str, Any]:
    """Directional late-chase verdict for one candidate ``row``.

    Rules:
      * long  + fresh impulse -> BLOCK  (chasing the breakout = late entry)
      * short + fresh impulse -> ALLOW  (fade into a rip is the short thesis)
      * no fresh impulse      -> ALLOW  (nothing to chase)

    ``mode="shadow"`` (default) NEVER blocks: a BLOCK decision is reported as
    ``SHADOW`` with ``would_block=True`` so reconciliation can log the counter-
    factual without affecting any live decision. ``mode="enforce"`` is required
    to surface an actual BLOCK, and enforce is not wired in this change.

    ``row`` is intentionally duck-typed to the decision_shadow_outcomes
    schema (only ``fresh_impulse`` is read), so historical rows replay directly.
    """
    if mode not in VALID_FRESHNESS_MODES:
        raise ValueError(f"unknown mode {mode!r}; expected {VALID_FRESHNESS_MODES}")
    if side not in VALID_SIDES:
        raise ValueError(f"unknown side {side!r}; expected {VALID_SIDES}")

    fresh = bool(row.get(fresh_key))
    directional_block = fresh and side == "long"
    decision = FRESHNESS_BLOCK if directional_block else FRESHNESS_ALLOW

    if mode == "shadow":
        return {
            "mode": "shadow",
            "side": side,
            "fresh_impulse": fresh,
            "decision": FRESHNESS_SHADOW,
            "would_block": directional_block,
            "enforce_decision": decision,
        }
    return {
        "mode": "enforce",
        "side": side,
        "fresh_impulse": fresh,
        "decision": decision,
        "would_block": directional_block,
        "enforce_decision": decision,
    }
