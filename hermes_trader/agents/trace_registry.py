"""Durable end-to-end analysis trace registry (audit 2026-10-15 P1).

Problem: a fill/close already persists ``analysis_id`` (shadow_book writes it
as ``trace_id``), but the originating analysis lived only in memory's rolling
list (``MAX_ANALYSES = 200``, a few hours at 60-market scan throughput), so by
the time a fill closes — or even later the same day — the analysis is evicted
and ``fill.analysis_id -> analyses.id`` joins at 0/290. The decision chain
(perception -> research verdict -> fill -> outcome -> reflection) could not be
reconstructed, so the 49 reflections were statistically unusable.

Fix: the single ``memory.record_analysis`` chokepoint also appends a SLIM,
durable ``analysis`` record to the tamper-evident events.jsonl (best-effort,
never blocks trading — the same pattern trades/closes/reflections already
use). The slim payload excludes the large reasoning/news blobs (they stay in
the hot rolling list / AI logs); it keeps exactly the fields needed to join a
fill back to its decision and to stratify outcomes. This module provides:

  * :func:`slim_analysis`     — project a full analysis to the durable shape;
  * :func:`record_analysis_event` — append the durable ``analysis`` event;
  * :func:`load_analysis_index`   — id/trace_id -> slim record over retention;
  * :func:`lookup_analysis`       — by analysis id or trace_id;
  * :func:`verify_trace`          — join closed fills back to their analysis and
                                    report matched / missing coverage.
"""
from __future__ import annotations

from typing import Any, Optional

# Fields kept in the durable ledger. Keep this list tight: it is written for
# every research verdict and must not bloat the rotating events.jsonl.
_SLIM_FIELDS = (
    "coin", "verdict", "confidence", "side", "entry_px", "stop_px", "tp_px",
    "created_at", "composite_score", "regime", "analyst_model",
    "debate_used", "debate_state", "ai_down", "nlp_parsed", "json_parsed",
    "degraded",
    "perception_id",
    # Audit 2026-10-25 §2.3: the fired-trigger set already exists on the full
    # in-memory analysis (research._build_analysis) but was dropped here, so
    # production events.jsonl could not attribute a surfacing to the trigger
    # that caused it without an offline replay. A compact list of short names
    # is cheap and bounded (projected defensively in slim_analysis).
    "fired_triggers",
)

# Trigger names are short camelCase tokens; cap the durable list so a malformed
# perception can never bloat the per-verdict ledger line.
_MAX_FIRED_TRIGGERS = 16


def _compact_fired_triggers(value: Any) -> list[str]:
    """Project a fired_triggers value to a bounded list[str] of short names."""
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for name in value:
        if not isinstance(name, str):
            continue
        token = name.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token[:48])
        if len(out) >= _MAX_FIRED_TRIGGERS:
            break
    return out


def slim_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Project a full in-memory analysis onto the durable join payload.

    Always carries the two join keys (``id``, ``trace_id``) even when absent on
    the source (empty string rather than KeyError), so a malformed analysis
    still lands and is auditable rather than silently dropped.
    """
    out: dict[str, Any] = {
        "id": str(analysis.get("id") or ""),
        "trace_id": str(analysis.get("trace_id") or ""),
    }
    for k in _SLIM_FIELDS:
        if k in analysis:
            out[k] = analysis[k]
    # Bounded, type-safe projection (never trust the perception payload shape).
    out["fired_triggers"] = _compact_fired_triggers(
        analysis.get("fired_triggers"))
    return out


def record_analysis_event(analysis: dict[str, Any]) -> bool:
    """Append the slim analysis to the durable, hash-chained event log.

    Best-effort: any failure is logged/swallowed inside event_log and returns
    False; the trading loop and the in-memory record are never affected.
    """
    try:
        from hermes_trader import event_log

        slim = slim_analysis(analysis)
        return bool(event_log.append(
            "analysis", payload=slim, trace_id=slim["id"]))
    except Exception:
        # Never let audit storage break research.
        return False


def _iter_analysis_events(start: Optional[str] = None,
                          end: Optional[str] = None) -> list[dict[str, Any]]:
    from hermes_trader import event_log

    return event_log.query_events(event_type="analysis", start=start, end=end)


def load_analysis_index(*, start: Optional[str] = None,
                        end: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Build ``id|trace_id -> slim record`` over the retained event history.

    Both keys map to the same record so a fill carrying either an analysis id
    or a research trace_id resolves. Later records win on a (rare) id clash.
    """
    index: dict[str, dict[str, Any]] = {}
    for ev in _iter_analysis_events(start=start, end=end):
        rec = ev.get("payload") or {}
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        tid = str(rec.get("trace_id") or "")
        if rid:
            index[rid] = rec
        if tid:
            index[tid] = rec
    return index


def lookup_analysis(analysis_id: str, *,
                    index: Optional[dict[str, dict[str, Any]]] = None,
                    start: Optional[str] = None,
                    end: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Resolve one fill/reflection trace id to its durable analysis record."""
    if not analysis_id:
        return None
    idx = index if index is not None else load_analysis_index(start=start, end=end)
    return idx.get(str(analysis_id))


def verify_trace(closed_fills: list[dict[str, Any]], *,
                 start: Optional[str] = None,
                 end: Optional[str] = None) -> dict[str, Any]:
    """Join closed fills back to the analysis that authorised them.

    ``closed_fills`` are the closed-fill rows (e.g. shadow_book closed
    positions); the join key is ``trace_id`` (shadow_book already sets
    ``trace_id = fill.analysis_id``) falling back to ``analysis_id``.

    Returns ``{total, with_key, matched, missing, coverage, missing_keys,
    sample_matched}``. ``missing`` counts fills that carried a key but whose
    analysis was evicted/predates the durable ledger — the audit gap. Fills
    with NO key at all are reported separately in ``with_key`` so a missing
    instrumentation point is distinguishable from an evicted analysis.
    """
    idx = load_analysis_index(start=start, end=end)
    total = len(closed_fills)
    with_key = 0
    matched_recs: list[dict[str, Any]] = []
    missing_keys: list[str] = []
    for f in closed_fills or []:
        key = str(f.get("trace_id") or f.get("analysis_id") or "")
        if not key:
            continue
        with_key += 1
        rec = idx.get(key)
        if rec is not None:
            matched_recs.append({"key": key, "coin": f.get("coin"),
                                 "verdict": rec.get("verdict"),
                                 "confidence": rec.get("confidence"),
                                 "side": rec.get("side")})
        else:
            missing_keys.append(key)
    matched = len(matched_recs)
    return {
        "total": total,
        "with_key": with_key,
        "keyless": total - with_key,
        "matched": matched,
        "missing": len(missing_keys),
        "coverage": round(matched / with_key, 4) if with_key else 0.0,
        "missing_keys": missing_keys,
        "sample_matched": matched_recs[:20],
        "index_size": len(idx),
    }
