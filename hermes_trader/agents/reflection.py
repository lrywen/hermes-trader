"""Post-close decision reflection (absorbed from TradingAgents).

After a position closes, this runs ONE lightweight LLM call — off the trading
critical path — to produce a short qualitative review of the decision: was the
direction read correct, was entry mistimed/late, did the gates behave sensibly.
Reflections are persisted (event_log + memory) and the most recent ones are
injected into the next research system prompt, closing a learn-from-outcomes
loop that previously only fed numeric win-rate back.

Strictly INERT: reflection never sizes, vetoes, or changes gates. Failure to
generate a reflection is swallowed so it can never affect trading.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REFLECTION_SYS = (
    "You are a senior trading reviewer. Given ONE closed crypto-perp trade — the "
    "signal/context at entry and the realized result — write a short, specific "
    "post-mortem. Focus on the DECISION, not hindsight luck: was the direction "
    "read justified by the evidence, was entry timing/late-entry a factor, and did "
    "the risk gates behave sensibly. State ONE concrete lesson to carry into the "
    "next similar setup. Do NOT recommend position sizing or account-risk changes "
    "(those are owned by fixed gates). Be concise and professional."
)


def reflection_cfg() -> dict[str, Any]:
    """Resolve reflection settings from agent config (canonical defaults)."""
    try:
        from hermes_trader.agents.config_store import read_agent_config

        r = read_agent_config().get("reflection") or {}
    except Exception:
        r = {}
    return {
        "enabled": bool(r.get("enabled", True)),
        "max_chars": int(r.get("max_chars", 400)),
        "inject_limit": int(r.get("inject_limit", 3)),
        "timeout_s": float(r.get("timeout_s", 20)),
    }


def _build_user_message(close: dict[str, Any]) -> str:
    """Project the self-contained close row into a compact reviewer brief."""
    sig = close.get("signals_at_entry") or {}
    enf = close.get("enforcement_at_entry") or {}
    brief = {
        "coin": close.get("coin"),
        "side": close.get("side"),
        "leverage": close.get("leverage"),
        "entry_px": close.get("entry_px"),
        "exit_px": close.get("exit_px"),
        "hold_minutes": close.get("hold_minutes"),
        "realized_pnl_pct_net": close.get("realized_pnl_pct"),
        "realized_pnl_usd_net": close.get("realized_pnl_usd"),
        "regime_at_entry": close.get("regime_at_entry"),
        "forced_override": close.get("forced_override"),
        "signals_at_entry": sig,
        "enforcement_at_entry": enf,
        "entry_slip_bps": close.get("entry_slip_bps"),
        "exit_slip_bps": close.get("exit_slip_bps"),
    }
    return (
        "Closed trade to review:\n"
        + json.dumps(brief, ensure_ascii=False, default=str)
        + "\n\nReturn ONLY the post-mortem text (no JSON, no headings), in "
        "Simplified Chinese."
    )


def generate_reflection(close: dict[str, Any]) -> Optional[str]:
    """Call the LLM once to review the closed trade. Returns text or None.

    Synchronous core; callers run it off the critical path (see
    :func:`maybe_reflect_async`).
    """
    cfg = reflection_cfg()
    if not cfg["enabled"]:
        return None
    # Only reflect on rows that carry an entry-time signal snapshot (positions
    # opened before instrumentation have nothing decision-level to review).
    if not close.get("signals_at_entry") and not close.get("entry_time"):
        return None
    try:
        from hermes_trader.agents.research import _call_openrouter

        text = _call_openrouter(
            _REFLECTION_SYS,
            _build_user_message(close),
            timeout=cfg["timeout_s"],
            max_tokens=int(cfg["max_chars"]),
            path="reflection",
        )
    except Exception as e:  # never let reflection break trading
        logger.warning(
            "[reflection] LLM call failed coin=%s: %s: %s",
            close.get("coin"), type(e).__name__, e,
        )
        return None
    text = (text or "").strip()
    if not text:
        return None
    # Strip code fences if the model wrapped prose anyway.
    if text.startswith("```"):
        text = text.strip("`").lstrip("\n")
        for marker in ("\n",):
            # drop a leading language tag line if present
            if text.split(marker, 1)[0].strip().isalpha() and marker in text:
                text = text.split(marker, 1)[1]
    return text[: cfg["max_chars"]].strip() or None


def _attach(close: dict[str, Any], text: str) -> None:
    """Persist the reflection: tag the close row in memory + emit event."""
    coin = close.get("coin")
    side = close.get("side")
    closed_at = close.get("closed_at")
    try:
        from hermes_trader.agents import memory

        memory.attach_reflection(coin, side, closed_at, text)
    except Exception as e:
        logger.warning("[reflection] memory attach failed coin=%s: %s", coin, e)
    try:
        from hermes_trader import event_log

        event_log.append(
            "reflection",
            payload={"coin": coin, "side": side, "closed_at": closed_at,
                     "text": text},
            trace_id=str(close.get("trace_id") or ""),
        )
    except Exception as e:
        logger.warning("[reflection] event_log append failed coin=%s: %s", coin, e)


def maybe_reflect_async(close: dict[str, Any]) -> None:
    """Fire a background reflection for a close; never blocks the caller.

    Uses the shared research thread pool. Any failure is contained inside the
    worker. Safe to call right after ``record_close``.
    """
    cfg = reflection_cfg()
    if not cfg["enabled"]:
        return
    try:
        from hermes_trader.agents.research import _get_pool

        def _job() -> None:
            try:
                text = generate_reflection(close)
                if text:
                    _attach(close, text)
            except Exception as e:  # containment: workers must not raise out
                logger.warning("[reflection] worker failed: %s: %s",
                               type(e).__name__, e)

        _get_pool().submit(_job)
    except Exception as e:
        logger.warning("[reflection] could not schedule review: %s: %s",
                       type(e).__name__, e)
