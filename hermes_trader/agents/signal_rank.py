"""Cross-signal ranking and selection (strategy-paradigm P0-1).

Hermes historically routes every triggered perception to research/execution
independently — first-come-first-served until the position cap — with no
comparison of *which* concurrent signal is better. This module adds a
deterministic, side-effect-free ranking layer placed AFTER the cheap gates
collect ``_research_jobs`` and BEFORE the paid LLM research, so the system can
both (a) prefer higher-quality setups under scarce slots/capital and
(b) avoid paying LLM cost for the deferred tail.

Everything here is a pure function over plain dicts/tuples: no network, no
clock, no mutation of inputs. The caller owns config gating
(off/shadow/enforce) and all auditable side effects (log rows, stamp
rollback). Scores are calibrated to a fixed 0–100 scale so the result is
stable regardless of the number of candidates.

Score components (each normalised to 0–100 before weighting), using only
fields available BEFORE research:

  - composite     : perception ``composite_score`` (already 0–100)
  - trigger_quality: number + individual scores of fired triggers
  - whale         : presence of an OI/funding anomaly whale signal
  - cvd           : directional CVD divergence strength
  - liquidity     : volume-profile read (prefers liquid, well-developed markets)
  - class_penalty : HIP-3 markets are scored slightly below the main crypto
                    universe (higher spread / lower depth risk)
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: Default component weights; sum is normalised at scoring time. Tunable via
#: the ``signal_ranking.score_weights`` config block. Kept conservative:
#: composite dominates, the rest are tie-breakers/quality nudges.
DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "composite": 0.55,
    "trigger_quality": 0.20,
    "whale": 0.08,
    "cvd": 0.07,
    "liquidity": 0.05,
    "class_penalty": 0.05,
}

#: HIP-3 markets get up to this many points subtracted from their final score
#: (``class_penalty`` component). Main crypto universe gets 0.
HIP3_CLASS_PENALTY = 60.0  # within the 0–100 class_penalty component

#: The seven perception trigger names treated as bullish (mirrors the inline
#: set in ta_filter.intend_long). Single source of truth for P0-1 ranking and
#: the P0-2 conjunction data collection.
BULLISH_TRIGGER_NAMES: frozenset[str] = frozenset({
    "breakout", "momentumBurst", "uptrendMomentum",
    "trendFlip1h", "higherLows1h", "volumeBuildup1h", "dailyMover",
})


# A research job is the 4-tuple ``(coin, perception, score, gate)`` assembled
# in trading_loop before the paid research phase.
Job = tuple[str, dict[str, Any], float, Any]


def _clamp01(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


def _fired_triggers(perception: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in perception.get("triggers", []) or []:
        if isinstance(h, dict) and h.get("fired"):
            out.append(h)
    return out


def _composite_component(perception: dict[str, Any], score: float) -> float:
    # Prefer the explicit trigger score passed by the loop; fall back to the
    # perception field. Clamp to 0–100 and never raise on bad data.
    v: float
    try:
        v = float(score)
    except (TypeError, ValueError):
        v = 0.0
    if v == 0.0:
        try:
            v = float(perception.get("composite_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
    return max(0.0, min(100.0, v))


def _trigger_quality_component(perception: dict[str, Any]) -> float:
    fired = _fired_triggers(perception)
    if not fired:
        return 0.0
    # More independently-fired triggers is stronger; cap the count contribution
    # so a single very-high-score trigger can still beat many weak ones.
    count_term = min(1.0, len(fired) / 3.0)
    # Mean of fired trigger scores, each expected on a 0–10 scale.
    score_vals = []
    for h in fired:
        try:
            score_vals.append(max(0.0, min(10.0, float(h.get("score", 0.0)))))
        except (TypeError, ValueError):
            score_vals.append(0.0)
    mean_trigger = (sum(score_vals) / len(score_vals)) / 10.0 if score_vals else 0.0
    # Blend: 60% trigger strength, 40% independent-confirmation breadth.
    return 100.0 * (0.6 * mean_trigger + 0.4 * count_term)


def _whale_component(perception: dict[str, Any]) -> float:
    whale = perception.get("whale_signal")
    if not whale:
        return 0.0
    # whale may be a bool or a structured anomaly dict; accept any truthy
    # signal but give a higher score when it carries an explicit magnitude.
    if isinstance(whale, dict):
        mag = whale.get("magnitude") or whale.get("strength") or whale.get("score")
        if mag is not None:
            return 100.0 * _clamp01(float(mag))
    return 70.0


def _cvd_component(perception: dict[str, Any]) -> float:
    cvd = perception.get("cvd_divergence")
    if not isinstance(cvd, dict) or not cvd:
        return 0.0
    strength = cvd.get("strength_pct") or cvd.get("strength") or 0.0
    try:
        # strength_pct may be expressed 0–100; normalise defensively.
        v = abs(float(strength))
    except (TypeError, ValueError):
        return 0.0
    if v <= 1.0:  # already fractional
        v *= 100.0
    return max(0.0, min(100.0, v))


def _liquidity_component(perception: dict[str, Any]) -> float:
    vp = perception.get("volume_profile")
    if not isinstance(vp, dict) or not vp:
        # No read: neutral rather than zero so absence of the optional block
        # doesn't systematically demote a coin.
        return 50.0
    # Prefer an explicit normalised strength/readiness field when present;
    # otherwise accept a present volume profile as moderately liquid.
    for key in ("readiness", "liquidity", "strength"):
        if key in vp:
            return 100.0 * _clamp01(vp[key] if float(vp[key]) <= 1.0
                                    else float(vp[key]) / 100.0)
    return 55.0


def _class_penalty_component(perception: dict[str, Any]) -> float:
    # Higher is WORSE here; combined as a subtraction weighted by class_penalty.
    if str(perception.get("type", "")).upper().startswith("HIP"):
        return HIP3_CLASS_PENALTY
    return 0.0


def _normalised_weights(weights: Mapping[str, float] | None = None) -> dict[str, float]:
    w = dict(DEFAULT_SCORE_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in w:
                try:
                    w[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    pass
    total = sum(w.values())
    if total <= 0:
        return dict(DEFAULT_SCORE_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def rank_score(
    perception: dict[str, Any],
    score: float,
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Return the calibrated 0–100 rank score for one candidate.

    Pure: no inputs are mutated and no external state is read.
    """
    w = _normalised_weights(weights)
    positive = (
        w["composite"] * _composite_component(perception, score)
        + w["trigger_quality"] * _trigger_quality_component(perception)
        + w["whale"] * _whale_component(perception)
        + w["cvd"] * _cvd_component(perception)
        + w["liquidity"] * _liquidity_component(perception)
    )
    penalty = w["class_penalty"] * _class_penalty_component(perception)
    return max(0.0, min(100.0, positive - penalty))


def rank_jobs(
    jobs: Sequence[Job],
    *,
    weights: dict[str, float] | None = None,
) -> list[tuple[Job, float]]:
    """Return ``(job, rank_score)`` pairs sorted best-first.

    Order is fully deterministic: descending score, then ascending coin name,
    so equal scores never depend on dict/iteration order.
    """
    scored = [(job, rank_score(job[1], job[2], weights=weights)) for job in jobs]
    scored.sort(key=lambda item: (-item[1], item[0][0]))
    return scored


def select_top_jobs(
    jobs: Sequence[Job],
    top_k: int,
    *,
    weights: dict[str, float] | None = None,
) -> tuple[list[Job], list[tuple[Job, float]]]:
    """Split jobs into the chosen top-``k`` and the ranked deferred remainder.

    ``top_k <= 0`` selects nothing; ``top_k >= len(jobs)`` selects everything.
    Returns ``(selected_jobs, deferred_with_scores)`` where the deferred list is
    ordered worst-first, matching the order callers iterate when logging each
    suppressed coin.
    """
    ranked = rank_jobs(jobs, weights=weights)
    if top_k <= 0:
        return [], ranked[::-1]
    if top_k >= len(ranked):
        return [j for j, _ in ranked], []
    selected = [j for j, _ in ranked[:top_k]]
    deferred = ranked[top_k:][::-1]
    return selected, deferred


def bullish_fired_count(perception: dict[str, Any]) -> int:
    """Count fired triggers whose names belong to the bullish set.

    Pure and defensive: malformed trigger entries are ignored.
    """
    n = 0
    for h in perception.get("triggers", []) or []:
        if (isinstance(h, dict) and h.get("fired")
                and h.get("name") in BULLISH_TRIGGER_NAMES):
            n += 1
    return n


def conjunction_view(
    perception: dict[str, Any],
    *,
    thresholds: Sequence[int] = (2, 3),
) -> dict[str, Any]:
    """Return the P0-2 conjunction counterfactual for one perception.

    Records how many bullish triggers fired and whether the signal would still
    pass under an AND-style requirement of >=k bullish triggers for each k.
    The production OR logic is never modified; this is observation only.
    """
    count = bullish_fired_count(perception)
    return {
        "bullish_fired": count,
        "passes_and": {str(int(k)): count >= int(k) for k in thresholds},
    }
