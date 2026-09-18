"""Durable audit recorder for the FORBIDDEN_OVERRIDE safety switches.

Moved verbatim (behavior-preserving) from ``agents/executor.py`` in the
P1-1 executor decomposition. This is not a gray-shadow feed — it writes a
durable ``force_override_armed`` event so post-trade review can reconstruct
which "skip a safety check" switch actually fired. Kept in the ``shadow``
subpackage because it is a best-effort, trade-path-safe audit recorder of
the same family as the shadow feeds.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# H3 (deep audit 2026-08-28): the six FORBIDDEN_OVERRIDE switches (see
# config_schema P0-3) arm "skip a safety check" paths. The schema refuses to
# arm any of them without override_requires_ai=true, but arming alone is
# silent at runtime — nothing in events.jsonl said "this gate was bypassed
# under a force override". Whenever the armed state is actually CONSULTED in
# a way that changes the decision (a PASS upgraded to LONG, the spread gate
# failing open, a whale signal clearing the counter-regime gate), this writes
# a durable force_override_armed line so post-trade review can reconstruct
# which switch fired. Best-effort: never blocks trading.
_FORCE_OVERRIDE_CONFIG_KEYS = (
    "composite_force_execute",
    "breakout_force_execute",
    "whale_force_execute",
    "ta_sidestep_force_execute",
    "whale_regime_bypass",
    "spread_gate_fail_open",
)


def _record_force_override_armed(*, coin: str, trigger: str,
                                 config: dict[str, Any],
                                 details: Optional[dict[str, Any]] = None,
                                 trace_id: str = "") -> None:
    """Durably record that an armed FORBIDDEN_OVERRIDE switch was consulted.

    Writes one ``force_override_armed`` event to events.jsonl carrying the
    trigger path, the armed switches (the P0-3 keys that are true in this
    config snapshot), the override_requires_ai state, and optional trigger
    detail (e.g. which structural-override sub-test fired)."""
    try:
        from hermes_trader import event_log
        armed = {
            k: bool(config.get(k, False))
            for k in _FORCE_OVERRIDE_CONFIG_KEYS
            if bool(config.get(k, False))
        }
        ok = event_log.append(
            "force_override_armed",
            payload={
                "coin": coin,
                "trigger": trigger,
                "armed_switches": armed,
                "override_requires_ai": bool(
                    config.get("override_requires_ai", True)),
                "details": details or {},
            },
            trace_id=trace_id,
        )
        if not ok:
            logger.error("[executor] force_override_armed event NOT durably "
                         "written for coin=%s trigger=%s — audit feed may "
                         "be down", coin, trigger)
    except Exception as e:
        logger.error("[executor] force_override_armed record raised %s: %s "
                     "(coin=%s trigger=%s)",
                     type(e).__name__, e, coin, trigger)
