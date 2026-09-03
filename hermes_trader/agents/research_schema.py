"""Structured research verdict — native replacement for HTA's Pydantic output.

Used by the in-process multi-perspective debate path (bull / bear / arbiter).
When the LLM is called with ``response_format`` set to
:meth:`ResearchVerdict.openrouter_response_format`, OpenRouter (when the model
supports it) returns strict JSON that validates against this model. Any failure
falls back to the legacy regex/JSON ``parse_verdict`` path — this module never
raises on bad input.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ResearchVerdict(BaseModel):
    """Schema for a single debate synthesis verdict."""

    verdict: Literal["LONG", "SHORT", "PASS"]
    confidence: float = Field(ge=0.0, le=1.0)
    conviction: Optional[Literal["low", "med", "high"]] = None
    thesis: str = ""
    bull_case: str = ""            # 多头论证（供审计/回测/下游风控）
    bear_case: str = ""            # 空头论证
    # Stop as a FRACTION of entry (e.g. 0.03 = 3% stop). Kept distinct from the
    # absolute stopPx/tpPx because the arbiter is context-free on price; the
    # caller resolves absolute levels from entry + ATR just like parse_verdict.
    suggested_stop_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    key_risks: list[str] = Field(default_factory=list)

    @classmethod
    def openrouter_response_format(cls) -> dict:
        """json_schema payload for an OpenRouter/OpenAI ``response_format``."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "verdict",
                "strict": True,
                "schema": cls.model_json_schema(),
            },
        }


def parse_structured(text: str) -> Optional[dict]:
    """Validate LLM JSON against :class:`ResearchVerdict`.

    Returns the validated dict on success, or ``None`` on any failure so the
    caller can fall back to the legacy ``parse_verdict`` regex path. Tolerates
    leading/trailing prose and ```json fences.
    """
    if not text:
        return None
    candidate = text.strip()
    # Strip code fences if present.
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
    candidate = re.sub(r"\s*```$", "", candidate)
    # If there is surrounding prose, grab the outermost {...} block.
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start : end + 1]
        else:
            return None
    try:
        return ResearchVerdict.model_validate_json(candidate).model_dump()
    except Exception:
        return None


def structured_to_analysis_fields(
    sv: dict[str, Any],
    coin: str,
    perception: dict[str, Any],
    *,
    atr_abs: Optional[float] = None,
    sl_atr_mult: float = 1.5,
    tp_atr_mult: float = 1.0,
) -> dict[str, Any]:
    """Map a validated structured verdict onto the fields ``research()`` needs.

    Mirrors ``parse_verdict``'s output contract (entry/stop/tp repair, side
    derivation, ATR fallback) so downstream consumers (risk_gates, executor)
    see an identical shape regardless of which path produced the verdict.
    """
    # local import avoids cycle at import time
    from .research import _atr_bracket, _coerce_px

    verdict = str(sv.get("verdict", "PASS")).upper()
    if verdict not in ("LONG", "SHORT", "PASS"):
        verdict = "PASS"

    try:
        confidence = float(sv.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    side = "long" if verdict == "LONG" else ("short" if verdict == "SHORT" else None)
    entry_px = _coerce_px(perception.get("mid", 0))
    entry_ref = entry_px

    # Build an auditable reasoning string that preserves both cases + thesis.
    reasoning_parts: list[str] = []
    if sv.get("thesis"):
        reasoning_parts.append(str(sv["thesis"]))
    if sv.get("bull_case"):
        reasoning_parts.append(f"BULL: {sv['bull_case']}")
    if sv.get("bear_case"):
        reasoning_parts.append(f"BEAR: {sv['bear_case']}")
    risks = sv.get("key_risks") or []
    if risks:
        reasoning_parts.append("RISKS: " + "; ".join(str(r) for r in risks))
    reasoning = "\n".join(reasoning_parts)[:1000]

    # Resolve stop/target: suggested_stop_pct (fraction) if given, else ATR.
    stop_px = 0.0
    tp_px = 0.0
    if verdict in ("LONG", "SHORT") and entry_ref > 0:
        is_long = verdict == "LONG"
        stop_pct = sv.get("suggested_stop_pct")
        try:
            stop_pct = float(stop_pct) if stop_pct is not None else None
        except (TypeError, ValueError):
            stop_pct = None
        if stop_pct and stop_pct > 0:
            stop_px = entry_ref * (1 - stop_pct) if is_long else entry_ref * (1 + stop_pct)
        elif _coerce_px(atr_abs) > 0:
            # P2-1: single ATR bracket formula shared with parse_verdict.
            stop_px, tp_px = _atr_bracket(entry_ref, _coerce_px(atr_abs),
                                          is_long, sl_atr_mult, tp_atr_mult)
        stop_px = max(0.0, stop_px)
        tp_px = max(0.0, tp_px)

    return {
        "verdict": verdict,
        "confidence": confidence,
        "side": side,
        "entry_px": entry_px,
        "stop_px": stop_px,
        "tp_px": tp_px,
        "news_risk": "none",
        "reasoning": reasoning,
        "ai_down": False,
        "nlp_parsed": False,
        "json_parsed": True,
        "structured": True,
        "conviction": sv.get("conviction"),
        "bull_case": sv.get("bull_case", ""),
        "bear_case": sv.get("bear_case", ""),
        "suggested_stop_pct": sv.get("suggested_stop_pct"),
        "key_risks": list(risks),
    }
