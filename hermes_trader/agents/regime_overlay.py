"""E1 (Q2, Audit 2026-09-06): regime_risk_overlay —— 震荡市自动降险总开关。

When the MACRO regime (BTC / equity proxy via market_regime.
detect_regime_with_score, which is TTL-cached) goes non-trending for several
consecutive samples, the book is auto-de-risked: max_concurrent -> 1,
allow_shorts -> false, equity_fraction_mult -> 0.5, pullback_long disabled.
After a run of trending samples the normal posture is restored.

Design constraints (per docs/remediation-plan-2026-09-06.md E1):
  * Hysteresis / debounce — a flip requires `hysteresis_bars` CONSECUTIVE
    same-class samples, so ADX wobbling around its chop threshold does not
    thrash the risk posture. The state machine is a PURE function
    (step_hysteresis) so it is fully unit-testable offline.
  * A "bar" is one fresh macro sample. Live wiring samples at most once per
    `sample_interval_s` (defaulted to the regime cache TTL) so a single scan
    over many coins — each of which calls into the gates — does NOT advance
    the counter N times on the same cached regime.
  * SHADOW first — shadow_mode flips the *posture* and records the
    counterfactual knobs to the audit JSONL, but `applied` is always False so
    live behaviour is unchanged until an operator removes shadow_mode.
  * Fail-safe — a regime lookup error HOLDS the prior posture/counter (it
    neither auto-derisks nor auto-restores). Overlay is strictly one-directional:
    it can only TIGHTEN the operator's base knobs (min / AND), never loosen them.

State is an in-process singleton (reset by tests / process restart). A restart
starts in normal posture and re-derisks only after a fresh confirmed run — no
derisk posture is persisted, which is the safe cold-start choice.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_trader.agents.market_regime import detect_regime_with_score

logger = logging.getLogger(__name__)

# A non-trending macro sample (chop = range/no trend, neutral = no direction)
# counts toward DERISK; an actual trend (up/down) counts toward RESTORE.
# "down" is a trend too — restoring shorts is what lets aligned shorts fire.
_NONTREND = ("chop", "neutral")
_TREND = ("up", "down")

_SHADOW_FILE_ENV = "HERMES_REGIME_OVERLAY_SHADOW_FILE"
_SHADOW_FILE_DEFAULT = "~/.hermes-trading/regime_overlay_shadow.jsonl"


@dataclass
class HystState:
    """Pure hysteresis state machine state.

    derisked      current posture: True = in the de-risked (chop) posture
    run_regime    regime class of the current consecutive run ("chop"/"trend")
    run_count     length of the current consecutive run
    """

    derisked: bool = False
    run_regime: Optional[str] = None
    run_count: int = 0
    last_regime: str = ""

    def reset(self) -> None:
        self.derisked = False
        self.run_regime = None
        self.run_count = 0
        self.last_regime = ""


def step_hysteresis(state: HystState, regime: Optional[str], bars: int) -> bool:
    """Advance the hysteresis state machine by ONE regime observation.

    Pure: mutates `state` in place and returns the new derisked flag.
    `regime` None/unknown means the lookup failed — hold the prior posture and
    counter (no flip, no reset).
    """
    if regime in _NONTREND:
        cls = "chop"
    elif regime in _TREND:
        cls = "trend"
    else:
        # Lookup error / unknown: hold state, do not touch counters.
        return state.derisked

    if cls == state.run_regime:
        state.run_count += 1
    else:
        state.run_regime = cls
        state.run_count = 1
    state.last_regime = regime

    need = max(1, int(bars))
    if cls == "chop":
        if not state.derisked and state.run_count >= need:
            state.derisked = True
    else:  # trend
        if state.derisked and state.run_count >= need:
            state.derisked = False
    return state.derisked


@dataclass
class OverlaySnapshot:
    enabled: bool = False
    shadow: bool = True
    derisked: bool = False        # posture the state machine has reached
    applied: bool = False         # True only when enforce AND derisked
    regime: str = ""
    run_regime: str = ""
    run_count: int = 0
    sampled: bool = False         # False = throttled by sample_interval_s
    block: dict = field(default_factory=dict)


# ── in-process singleton state ──────────────────────────────────────────────

_state = HystState()
_last_sample_ts = 0.0


def reset_overlay_state() -> None:
    """Reset the singleton (tests / cold start)."""
    global _last_sample_ts
    _state.reset()
    _last_sample_ts = 0.0


def get_overlay_state() -> HystState:
    return _state


def _shadow_path(blk: dict) -> str:
    return str(blk.get("shadow_log_path") or "").strip() or os.environ.get(
        _SHADOW_FILE_ENV, os.path.expanduser(_SHADOW_FILE_DEFAULT))


def _record_overlay(rec: dict, path: str) -> None:
    """Best-effort append an overlay event to its audit JSONL.

    Audit 2026-09-06 (F2): routed through the shared shadow_log writer
    (size-based rotation + write-failure metric).
    """
    from hermes_trader.shadow_log import append_jsonl

    append_jsonl(path, rec, stream="regime_overlay")


def evaluate_risk_overlay(config: Optional[dict], coin: str = "BTC") -> OverlaySnapshot:
    """Evaluate the book-level macro overlay and advance its hysteresis state.

    Returns an OverlaySnapshot. When disabled, applied is always False and the
    posture is normal. Callers feed the snapshot to resolve_applied_knobs() to
    get the effective (possibly de-risked) knobs; in shadow_mode those knobs
    are never tightened (applied=False) and the counterfactual is logged.
    """
    blk = (config or {}).get("regime_risk_overlay")
    if not isinstance(blk, dict) or not bool(blk.get("enabled", False)):
        return OverlaySnapshot(enabled=False)

    shadow = bool(blk.get("shadow_mode", True))
    bars = int(blk.get("hysteresis_bars", 3) or 3)
    sample_interval = float(blk.get("sample_interval_s", 300.0) or 0.0)

    snap = OverlaySnapshot(enabled=True, shadow=shadow, block=blk,
                           derisked=_state.derisked, regime=_state.last_regime,
                           run_regime=_state.run_regime or "", run_count=_state.run_count)

    # Sample-rate throttle: at most one state-machine step per fresh macro
    # sample (the detect call itself is TTL-cached, but multiple coins in a
    # scan must not each advance the counter on the same cached reading).
    global _last_sample_ts
    now = time.time()
    if _last_sample_ts and sample_interval > 0 and \
            (now - _last_sample_ts) < sample_interval:
        snap.sampled = False
        snap.applied = bool(not shadow and _state.derisked)
        return snap
    _last_sample_ts = now
    snap.sampled = True

    regime = ""
    try:
        regime, _score = detect_regime_with_score(coin)
    except Exception as e:
        # Fail-safe: hold prior posture/counter; wait for the next valid sample.
        logger.warning(f"[regime_overlay] macro regime lookup failed "
                       f"({coin}): {e} — holding current posture "
                       f"(derisked={_state.derisked})")
        snap.applied = bool(not shadow and _state.derisked)
        return snap

    before = _state.derisked
    derisked = step_hysteresis(_state, regime, bars)
    snap.derisked = derisked
    snap.regime = regime
    snap.run_regime = _state.run_regime or ""
    snap.run_count = _state.run_count
    snap.applied = bool(not shadow and derisked)

    if derisked != before:
        event = "enter_derisk" if derisked else "exit_derisk"
        active = "chop" if derisked else "trend"
        profile = blk.get(active) if isinstance(blk.get(active), dict) else {}
        mode = "shadow" if shadow else "enforce"
        rec = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
            "mode": mode,
            "regime": regime,
            "active_profile": active,
            "run_count": _state.run_count,
            "hysteresis_bars": bars,
            # In shadow the knobs would be applied but are NOT (counterfactual).
            "would_apply": bool(shadow and derisked),
            "applied": snap.applied,
            "knobs": {
                "max_concurrent": profile.get("max_concurrent"),
                "allow_shorts": profile.get("allow_shorts"),
                "equity_fraction_mult": profile.get("equity_fraction_mult"),
                "pullback_long_enabled": profile.get("pullback_long_enabled"),
            },
        }
        _record_overlay(rec, _shadow_path(blk))
        if derisked:
            logger.warning(
                f"[regime_overlay] ENTER de-risk posture after "
                f"{_state.run_count} consecutive non-trending macro samples "
                f"(regime={regime}) — {mode.upper()}: "
                f"max_concurrent<= {profile.get('max_concurrent')}, "
                f"shorts off, equity x{profile.get('equity_fraction_mult')}, "
                f"pullback_long off")
        else:
            logger.info(
                f"[regime_overlay] EXIT de-risk posture after "
                f"{_state.run_count} consecutive trending macro samples "
                f"(regime={regime}) — normal posture restored ({mode.upper()})")

    return snap


def apply_overlay_knobs(base: dict, blk: dict, derisked: bool) -> dict:
    """Pure knob resolver: apply the ACTIVE overlay profile onto `base`.

    `derisked=True` selects the chop profile, `False` the trend profile. The
    overlay may only TIGHTEN the operator's base knobs — max_concurrent takes
    the smaller value, booleans use AND, equity_fraction_mult is clamped into
    (0, 1] then takes the smaller value — so even the trend profile never
    loosens the operator baseline. This is a pure function (no I/O, no state).
    """
    out = dict(base)
    blk = blk if isinstance(blk, dict) else {}
    active = "chop" if derisked else "trend"
    prof = blk.get(active)
    if not isinstance(prof, dict):
        prof = {}

    # max_concurrent: take the smaller (never raise the operator's cap).
    mc = prof.get("max_concurrent")
    if mc is not None:
        try:
            out["max_concurrent"] = min(int(out["max_concurrent"]), int(mc))
        except (TypeError, ValueError):
            pass
    # allow_shorts: AND (overlay can only disable shorts, never enable).
    if prof.get("allow_shorts") is not None:
        out["allow_shorts"] = bool(out["allow_shorts"]) and bool(prof["allow_shorts"])
    # equity_fraction_mult: clamp into (0, 1] then take the smaller (size down only).
    mult = prof.get("equity_fraction_mult")
    if mult is not None:
        try:
            m = max(0.0, min(1.0, float(mult)))
            out["equity_fraction_mult"] = min(
                float(out.get("equity_fraction_mult", 1.0)), m)
        except (TypeError, ValueError):
            pass
    # pullback_long_enabled: AND (only can force-disable).
    if prof.get("pullback_long_enabled") is not None:
        out["pullback_long_enabled"] = (
            bool(out["pullback_long_enabled"])
            and bool(prof["pullback_long_enabled"]))
    return out


def resolve_applied_knobs(base: dict, config: Optional[dict],
                          snap: Optional[OverlaySnapshot] = None) -> dict:
    """Resolve the effective knobs given the overlay posture snapshot.

    `base` holds the operator-configured values for the four keys. The overlay
    only tightens them when the snapshot is enabled AND applied (enforce mode
    and de-risked posture). In shadow mode or when disabled/normal the base
    values are returned untouched so live behaviour is unchanged.
    """
    out = dict(base)
    if snap is None:
        snap = OverlaySnapshot(enabled=False)
    if not snap.enabled or not snap.applied:
        return out

    blk = snap.block if isinstance(snap.block, dict) else \
        (config or {}).get("regime_risk_overlay")
    return apply_overlay_knobs(out, blk, snap.derisked)
