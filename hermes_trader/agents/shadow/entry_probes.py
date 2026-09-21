"""Entry-path shadow/counterfactual audit recorders.

Moved verbatim (behavior-preserving) from ``agents/executor.py`` in the
P1-1 executor decomposition. Each recorder appends a best-effort JSONL line
(via the shared rotating ``hermes_trader.shadow_log`` writer) describing a
counter-factual that the LIVE decision deliberately does not act on. None of
these ever place an order, change the live block decision, or raise into the
trade hot path.

Clusters:
  * pullback-long bypass shadow
  * generic risk-tuning shadow (per-rule ``rule`` tag; shared by several arms)

2026-09-21 cleanup: the short-only / early-breakout / per-coin-regime shadow
streams were removed as redundant orphans (no decision consumer under the
mechanically-confirmed OUTCOME_B), along with their writers and recon scripts.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


_PULLBACK_SHADOW_FILE = os.environ.get(
    "HERMES_PULLBACK_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/pullback_shadow.jsonl"),
)


def _record_pullback_shadow(*, coin: str, side: str, score: float,
                            conf: float, slow_count: int,
                            rsi4h: Any, extension_atr: Any,
                            entry_px: float, trace_id: str = "",
                            macro_regime: str = "") -> None:
    """Best-effort append a pullback-long shadow signal to the audit JSONL."""
    from datetime import datetime, timezone
    rec = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trace_id": trace_id or "",
        "coin": coin,
        "side": side,
        "entry_px": float(entry_px or 0.0),
        "composite_score": float(score),
        "confidence": float(conf),
        "slow_burn_count": int(slow_count),
        "rsi4h": (float(rsi4h) if rsi4h is not None else None),
        "extension_atr": (float(extension_atr) if extension_atr is not None else None),
        # Audit 2026-09-06 (E2): macro regime (up/chop/neutral/down) recorded so
        # reconciliation can split outcomes by macro regime.
        "macro_regime": macro_regime or "",
        "outcome": None,  # filled by reconciliation script
        "exit_px": None,
        "pnl_usd": None,
    }
    # Audit 2026-09-06 (F2): routed through the shared shadow_log writer
    # (size-based rotation + write-failure metric).
    from hermes_trader.shadow_log import append_jsonl

    if append_jsonl(_PULLBACK_SHADOW_FILE, rec, stream="pullback"):
        logger.info(f"[executor] pullback-long SHADOW recorded for {coin} "
                    f"-> {_PULLBACK_SHADOW_FILE}")


# ── Risk-tuning shadow audit ─────────────────────────────────────────────
# Proposed behaviour changes are run in SHADOW first. When enabled, every
# candidate that WOULD be blocked / de-levered / re-armed by a proposed rule is
# appended to ONE JSONL with ``rule`` discriminator, while the live decision is
# unchanged. A reconciliation pass over this feed decides which rule is safe to
# flip to enforce (same shared rotating writer, never raises into the trade
# path). This recorder is shared by several arms (e.g. leverage_tier), so it
# stays even though the risk-tuning page itself was removed.
_RISK_TUNING_SHADOW_FILE = os.environ.get(
    "HERMES_RISK_TUNING_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/risk_tuning_shadow.jsonl"),
)


def _record_risk_tuning_shadow(*, rule: str, coin: str, side: str,
                               would: str, detail: dict[str, Any],
                               trace_id: str = "") -> None:
    """Best-effort append a risk-tuning shadow verdict.

    ``would`` is the counter-factual action ("block" / "deleverage" /
    "rearm_stop"); ``detail`` carries the observed vs threshold values needed
    for offline reconciliation. Never raises into the entry hot path.
    """
    from datetime import datetime, timezone
    rec = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trace_id": trace_id,
        "rule": rule,
        "coin": coin,
        "side": side,
        "would": would,
        "detail": detail,
        "outcome": None,      # filled by reconciliation
        "exit_px": None,
        "pnl_usd": None,
    }
    from hermes_trader.shadow_log import append_jsonl

    if append_jsonl(_RISK_TUNING_SHADOW_FILE, rec, stream="risk_tuning"):
        logger.info(f"[executor] risk-tuning SHADOW[{rule}] for {coin}: "
                    f"would={would} -> {_RISK_TUNING_SHADOW_FILE}")
