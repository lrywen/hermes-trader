"""Record-only pre-research shadow for the 40-vs-45 composite gate question.

Background (v11 §2.1, offline counterfactual ch_weight_calibration 2026-10-24):
after trend_strength weight 0.55 -> 0.30 while min_composite stayed 45, setups
scoring in the [40, 45) band never reach the paid research LLM — they are shed
upstream of the executor runner gate (which only runs AFTER a LONG/SHORT
verdict exists). There is therefore no historical AI verdict for that band, so
the gate cannot be recalibrated on AI-gated evidence.

This module buys ONE isolated research verdict for such a candidate and writes
it to a standalone JSONL. It is strictly observational:

  * the verdict is returned to nobody — it is NOT routed, does not place or
    shadow-book an order, and does not touch any cooldown / research-throttle /
    dedup state in the main flow;
  * the main flow decides independently whether the candidate is researched;
  * every failure degrades to a no-op (the shadow must never perturb trading).

Configuration (runner_entry_gate.candidate_gate_shadow):
  enabled bool (default False — the whole path is a strict no-op when off)
  min_composite / max_composite: the half-open score band [lo, hi)
  log_path: optional JSONL override (defaults to candidate_gate_shadow.jsonl)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional, Set

logger = logging.getLogger(__name__)

# Module-level fingerprint set so the same closed-bar setup is shadow-researched
# at most once per process even if it keeps surfacing for cycles. Keyed on the
# same signal_fingerprint the main content-dedup uses (bar_close + triggers).
_SEEN_FPS: Set[str] = set()
_MAX_SEEN = 5_000  # bound memory: once exceeded, clear and re-learn


def _resolve_band(config: dict[str, Any]) -> Optional[tuple[float, float, str]]:
    """Return (lo, hi, log_path) when the arm is enabled and well-formed."""
    gate = (config or {}).get("runner_entry_gate") or {}
    cfg = gate.get("candidate_gate_shadow") or {}
    if not bool(cfg.get("enabled", False)):
        return None
    try:
        lo = float(cfg.get("min_composite", 40.0))
        hi = float(cfg.get("max_composite", 45.0))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= lo < hi <= 100.0):
        return None
    return lo, hi, str(cfg.get("log_path") or "")


def in_candidate_band(
    *, score: float, ta_signal: str, intend_long: bool,
    held: bool, config: dict[str, Any],
) -> bool:
    """Pure eligibility predicate (no LLM, no I/O). Unit-tested directly.

    Long-only by construction (matches the production allow_shorts=false state):
    only long-intent, non-held, non-REJECTED candidates whose composite is in
    the configured half-open band qualify. A REJECTED TA verdict (which already
    includes the late-entry veto) is never shadow-researched.
    """
    band = _resolve_band(config)
    if band is None:
        return False
    lo, hi, _ = band
    if held or not intend_long:
        return False
    if ta_signal == "REJECTED":
        return False
    try:
        s = float(score)
    except (TypeError, ValueError):
        return False
    return lo <= s < hi


def maybe_shadow_research(
    *, coin: str, perception: dict[str, Any], score: float,
    ta: dict[str, Any], held: bool, fingerprint: Optional[str],
    config: dict[str, Any], data_dir: str,
    research_fn: Callable[..., dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Run one isolated research verdict for an eligible candidate; record-only.

    Returns the written record (or None when skipped). Never raises. The
    ``research_fn`` is injected so tests can supply a stub; production passes
    ``hermes_trader.agents.research.research`` (LLM + read-only prefetch, no
    order/state side effects).
    """
    try:
        if not in_candidate_band(
                score=score,
                ta_signal=str((ta or {}).get("signal") or ""),
                intend_long=bool((ta or {}).get("intend_long")),
                held=bool(held), config=config):
            return None
        # Content dedup: never re-pay for the same closed-bar setup.
        if fingerprint is not None:
            if fingerprint in _SEEN_FPS:
                return None
            if len(_SEEN_FPS) >= _MAX_SEEN:
                _SEEN_FPS.clear()
            _SEEN_FPS.add(fingerprint)

        band = _resolve_band(config)
        assert band is not None
        lo, hi, log_path = band
        now_ms = int(time.time() * 1000)
        analysis: dict[str, Any] = {}
        err = None
        try:
            analysis = research_fn(coin, perception) or {}
        except Exception as e:  # LLM/network failure: record the miss, never raise
            err = f"{type(e).__name__}: {repr(e) if str(e) == '' else str(e)}"

        rec = {
            "ts": now_ms,
            "coin": coin,
            "composite_score": round(float(score), 3),
            "band_lo": lo,
            "band_hi": hi,
            "ta_signal": (ta or {}).get("signal"),
            "ta_score": (ta or {}).get("score"),
            "fingerprint": fingerprint,
            "shadow": True,                 # never a trade / routing decision
            "research_error": err,
            "verdict": analysis.get("verdict"),
            "confidence": analysis.get("confidence"),
            "side": analysis.get("side"),
            "ai_confidence_raw": analysis.get("ai_confidence_raw"),
            "analysis_id": analysis.get("id"),
            "fired_triggers": perception.get("fired_triggers")
            or perception.get("triggers"),
        }
        path = log_path or os.path.join(data_dir, "candidate_gate_shadow.jsonl")
        from hermes_trader.shadow_log import append_jsonl
        append_jsonl(path, rec, stream="candidate_gate_shadow")
        if err:
            logger.warning(f"[candidate_gate_shadow] {coin} research failed "
                           f"(record-only, no trade): {err}")
        else:
            logger.info(f"[candidate_gate_shadow] {coin} score={score:.1f} "
                        f"in [{lo:.0f},{hi:.0f}) -> shadow verdict="
                        f"{rec['verdict']} conf={rec['confidence']} (no trade)")
        return rec
    except Exception as e:  # absolute guard: the shadow never breaks the loop
        logger.debug("[candidate_gate_shadow] skipped: %s", e)
        return None


def reset_seen_for_tests() -> None:
    _SEEN_FPS.clear()
