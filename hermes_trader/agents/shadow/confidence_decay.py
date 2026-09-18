"""AI-confidence freshness decay gray-release (off / shadow / enforce).

Moved verbatim (behavior-preserving) from ``agents/executor.py`` in the
P1-1 executor decomposition. Exponentially decays AI confidence by the age
of the verdict CONTENT (first time this exact verdict/side/confidence was
seen for the coin): factor = 2 ** (-age/hl). The technical-trigger half lives
in perception (composite_score_aged); this covers the AI conviction driving
the confidence gate / conviction sizing.

Modes:
  off     — confidence never touched; byte-identical behaviour.
  shadow  — decayed confidence computed + logged, analysis keeps raw value.
  enforce — analysis confidence multiplied by the factor before any
            gate/sizer reads it (raw kept in ai_confidence_pre_decay).

The two module-level singletons ``_confidence_decay_lock`` /
``_confidence_decay_onset`` are the SAME objects re-exported on
``agents.executor`` so in-process state and tests that inject onset entries
keep working across the extraction.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from hermes_trader.agents.config_store import report_legacy_mode_drift
from hermes_trader.indicators.triggers import decay_factor

logger = logging.getLogger(__name__)

_CONFIDENCE_DECAY_MODES = ("off", "shadow", "enforce")
# Default verdict half-life: 15 min (900s) — same order as the breakout
# trigger half-life and well beyond a fresh 1-3 min entry window. Tunable via
# the confidence_decay block; calibrated from shadow data.
_CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S = 900.0
# Prune onset state older than 24h (defensive; cache TTL bounds the real age).
_CONFIDENCE_DECAY_ONSET_TTL_S = 24 * 3600.0

_confidence_decay_lock = threading.Lock()
# coin -> {"sig": verdict signature, "first_ts": epoch seconds}
_confidence_decay_onset: dict[str, dict[str, Any]] = {}


def _reset_confidence_decay() -> None:
    """Clear verdict-onset state (tests / explicit reset)."""
    with _confidence_decay_lock:
        _confidence_decay_onset.clear()


def _confidence_decay_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the AI-confidence freshness-decay block.

    Sources in priority order: env ``HERMES_CONFIDENCE_DECAY_MODE`` (gray-
    release flip without a file write), then ``confidence_decay.mode`` in the
    merged agent config. Invalid values fall back to off (safe default).
    """
    blk = config.get("confidence_decay") or {}
    file_mode = str(blk.get("mode") or "").strip().lower()
    if file_mode not in _CONFIDENCE_DECAY_MODES:
        file_mode = "off"
    env_mode = str(os.environ.get("HERMES_CONFIDENCE_DECAY_MODE") or "").strip().lower()
    mode = env_mode if env_mode else file_mode
    if mode not in _CONFIDENCE_DECAY_MODES:
        mode = "off"
    report_legacy_mode_drift(
        env_name="HERMES_CONFIDENCE_DECAY_MODE",
        env_mode=env_mode,
        file_key="confidence_decay.mode",
        file_mode=file_mode,
        valid_modes=_CONFIDENCE_DECAY_MODES,
    )
    try:
        halflife_s = max(0.0, float(blk.get("halflife_s", _CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S)))
    except (TypeError, ValueError):
        halflife_s = _CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S
    return {"mode": mode, "halflife_s": halflife_s, "block": blk}


def _verdict_signature(analysis: dict[str, Any]) -> str:
    """Content signature of the AI verdict whose age we track.

    Keys on verdict + side + rounded confidence: the debate cache returns the
    same verdict tuple on a hit (genuinely stale), while a fresh LLM call that
    changed its mind or its confidence produces a new signature and thus
    restarts age at 0. News/whale catalysts feed the LLM prompt, so a changed
    catalyst moves the confidence/verdict and naturally resets the clock.
    """
    verdict = str(analysis.get("verdict") or "").upper()
    side = str(analysis.get("side") or "").lower()
    try:
        conf = round(float(analysis.get("confidence", 0) or 0), 2)
    except (TypeError, ValueError):
        conf = 0.0
    return f"{verdict}|{side}|{conf:.2f}"


def _confidence_decay_age_s(coin: str, sig: str, now: float) -> float:
    """Return the age in seconds of this (coin, verdict-signature) content.

    First sighting (or a changed signature) stamps the current time → age 0.
    """
    with _confidence_decay_lock:
        # Defensive prune of ancient entries (long gap / restart).
        stale = [c for c, v in _confidence_decay_onset.items()
                 if (now - float(v.get("first_ts", now))) > _CONFIDENCE_DECAY_ONSET_TTL_S]
        for c in stale:
            del _confidence_decay_onset[c]
        entry = _confidence_decay_onset.get(coin)
        if entry is None or entry.get("sig") != sig:
            _confidence_decay_onset[coin] = {"sig": sig, "first_ts": now}
            return 0.0
        return max(0.0, now - float(entry["first_ts"]))


def _confidence_decay_shadow_path(blk: dict[str, Any]) -> str:
    """Resolve the confidence-decay shadow JSONL path (config → env → default)."""
    return str(blk.get("shadow_log_path") or "").strip() or os.environ.get(
        "HERMES_CONFIDENCE_DECAY_SHADOW_FILE",
        os.path.expanduser("~/.hermes-trading/confidence_decay_shadow.jsonl"),
    )


def _confidence_decay_record_shadow(rec: dict[str, Any], path: str) -> None:
    """Best-effort append a confidence-decay shadow record to the JSONL.

    Audit 2026-09-06 (F2): routed through the shared shadow_log writer
    (size-based rotation + write-failure metric).
    """
    from hermes_trader.shadow_log import append_jsonl

    append_jsonl(path, rec, stream="confidence_decay")


def _confidence_decay_metric(mode: str, outcome: str) -> None:
    """Best-effort Prometheus counter (roadmap R7 registration)."""
    try:
        from hermes_trader import metrics
        metrics.CONFIDENCE_DECAY_OBSERVATIONS.labels(mode=mode, outcome=outcome).inc()
    except Exception:
        pass


def _apply_confidence_decay(analysis: dict[str, Any], config: dict[str, Any]) -> None:
    """Apply the AI-confidence freshness decay in place (roadmap §2).

    Mutates ``analysis["confidence"]`` only in enforce mode; shadow mode only
    observes and writes the JSONL. Structural-override (PASS → LONG) verdicts
    are skipped: those entries are driven by live technical/whale structure
    (already covered by the perception trigger age-decay), not by the AI's
    conviction, so decaying them would launder nothing but could block a
    fresh structural setup. Only the model's own directional LONG/SHORT
    convictions are aged.
    """
    cfg = _confidence_decay_config(config)
    mode = cfg["mode"]
    if mode == "off":
        return
    verdict = str(analysis.get("verdict") or "").upper()
    if verdict not in ("LONG", "SHORT"):
        return
    coin = str(analysis.get("coin") or "")
    raw = float(analysis.get("confidence", 0) or 0)
    sig = _verdict_signature(analysis)
    age_s = _confidence_decay_age_s(coin, sig, time.time())
    factor = decay_factor(age_s * 1000.0, cfg["halflife_s"] * 1000.0)
    decayed = raw * factor
    # Outcome label: would_block (shadow counterfactual) / applied (factor
    # actually bites) / no_change (fresh verdict, factor ≈ 1).
    _outcome = "applied" if factor < 0.999 else "no_change"
    # Confidence gate threshold for the counterfactual "would this entry have
    # been blocked?" tag (aligned with risk_gates' min_ai_confidence read).
    try:
        min_conf = float(config.get("min_ai_confidence", 0.70) or 0.70)
    except (TypeError, ValueError):
        min_conf = 0.70
    if mode == "enforce":
        # Keep the pre-decay conviction for audit; override/runner code that
        # wants the model's raw opinion reads ai_confidence_raw (set later by
        # the override path on its own terms).
        analysis["ai_confidence_pre_decay"] = raw
        analysis["confidence_decay"] = {
            "age_s": round(age_s, 1), "halflife_s": round(cfg["halflife_s"], 1),
            "factor": round(factor, 4),
        }
        analysis["confidence"] = decayed
    try:
        _confidence_decay_record_shadow({
            "ts": int(time.time() * 1000),
            "mode": mode,
            "coin": coin,
            "verdict": verdict,
            "side": str(analysis.get("side") or "").lower(),
            "confidence_raw": round(raw, 4),
            "confidence_decayed": round(decayed, 4),
            "age_s": round(age_s, 1),
            "halflife_s": round(cfg["halflife_s"], 1),
            "decay_factor": round(factor, 4),
            "min_confidence": round(min_conf, 4),
            "would_block_gate": bool(raw >= min_conf and decayed < min_conf),
            "debate_used": bool(analysis.get("debate_used", False)),
        }, _confidence_decay_shadow_path(cfg["block"]))
    except Exception as e:
        logger.debug(f"[confidence-decay] shadow record failed for {coin}: {e}")
    _confidence_decay_metric(
        mode, "would_block" if bool(raw >= min_conf and decayed < min_conf) else _outcome)
