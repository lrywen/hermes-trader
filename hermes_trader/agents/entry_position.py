"""Entry-position hard gate + terminal-blowoff latch (anti-high-chase).

Why this exists
---------------
The ``late_chase`` real-time leg blocked a vertical move *while* its live RSI /
ATR-extension sat beyond the trigger, but the comparison was a single-point
snapshot with no memory. On 2026-10-02 AAVE the gate blocked four consecutive
cycles (live RSI 78-83 / ext 4.1-4.5 ATR) and then **admitted on the next
cycle** the moment both values dipped a hair under the trigger (RSI 79,
ext 2.9) — while price was still at the 89th percentile of its 24h range.
Racing a parabolic that merely *pauses* near its high is not safe. Price then
never went higher and the paper long stopped for -1.5% spot.

Two structural fixes live here:

1. ``range_percentile`` — where the live price sits within its recent
   high/low window, in percent. A long above ``block_high_pct`` (or a short
   below ``block_low_pct``) is a top/bottom-tick chase **regardless of RSI**.
   This closes the "RSI not extreme but price still at the high" hole.
2. ``BlowoffLatch`` — once a coin's real-time RSI/extension reaches a terminal
   extreme, the latch is SET for that direction and keeps same-direction
   chasing blocked until a real *release* condition (RSI back to a neutral
   band, or the cooldown elapses), not merely the instant the value crosses
   back under the trigger. Hysteresis replaces the single-point comparison.

Pure helpers + a tiny persisted state (atomic write, fail-open). No network
inside the percentile math — callers supply candles.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from hermes_trader.agents.atomic_io import write_json_atomic

logger = logging.getLogger(__name__)

STATE_FILE = os.environ.get(
    "HERMES_BLOWOFF_LATCH_FILE", "/data/.blowoff-latch.json"
)
_STATE_VERSION = 1


# ── Range percentile ────────────────────────────────────────────────────────

def range_percentile(
    highs: list[float], lows: list[float], price: float
) -> Optional[float]:
    """Percentile (0-100) of ``price`` within the supplied high/low window.

    0 = at the window low, 100 = at the window high. Returns None when the
    window is empty/degenerate or price invalid."""
    try:
        if not highs or not lows or len(highs) != len(lows):
            return None
        hi = max(highs)
        lo = min(lows)
        p = float(price)
        if hi <= lo or p != p:
            return None
        return (p - lo) / (hi - lo) * 100.0
    except (TypeError, ValueError):
        return None


def extreme_position_block(
    *,
    side: str,
    percentile: Optional[float],
    block_high_pct: float,
    block_low_pct: float,
) -> Optional[str]:
    """Return a block reason when a long/short enters at an extreme percentile,
    else None. Thresholds <= 0 disable that side."""
    if percentile is None:
        return None
    if side == "long" and block_high_pct > 0 and percentile >= block_high_pct:
        return (f"entry at {percentile:.0f}th pct of lookback range "
                f"(>= {block_high_pct:.0f}) — top-tick long chase")
    if side == "short" and block_low_pct > 0 and percentile <= block_low_pct:
        return (f"entry at {percentile:.0f}th pct of lookback range "
                f"(<= {block_low_pct:.0f}) — bottom-tick short chase")
    return None


# ── Blowoff latch (hysteresis) ──────────────────────────────────────────────

def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("latches"), dict):
            return data
    except (FileNotFoundError, ValueError, OSError, TypeError):
        pass
    return {"version": _STATE_VERSION, "latches": {}}


def _latch_key(coin: str, direction: str) -> str:
    return f"{coin}|{direction}"


def set_blowoff(
    *,
    coin: str,
    direction: str,
    now_ms: Optional[int] = None,
    path: Optional[str] = None,
) -> None:
    """Latch ``direction`` ('up' for a long blowoff, 'down' for short) as
    terminal. Idempotent while already latched. Best-effort."""
    target = path or STATE_FILE
    try:
        if direction not in ("up", "down"):
            return
        state = _load(target)
        latches = state["latches"]
        key = _latch_key(str(coin)[:32], direction)
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        rec = latches.get(key)
        if not isinstance(rec, dict):
            rec = {"since_ms": now_ms}
        rec["updated_ms"] = now_ms
        latches[key] = rec
        state["latches"] = latches
        write_json_atomic(target, state, indent=None, fsync=False)
    except Exception as e:  # never perturb the order path
        logger.debug("[blowoff_latch] set failed for %s/%s: %s",
                     coin, direction, e)


def clear_blowoff(
    *,
    coin: str,
    direction: str,
    path: Optional[str] = None,
) -> None:
    """Remove a latch once its release condition is met. Best-effort."""
    target = path or STATE_FILE
    try:
        state = _load(target)
        latches = state["latches"]
        key = _latch_key(str(coin)[:32], direction)
        if key in latches:
            latches.pop(key, None)
            state["latches"] = latches
            write_json_atomic(target, state, indent=None, fsync=False)
    except Exception as e:
        logger.debug("[blowoff_latch] clear failed for %s/%s: %s",
                     coin, direction, e)


def is_blowoff_latched(
    *,
    coin: str,
    direction: str,
    now_ms: Optional[int] = None,
    max_age_ms: float = 0.0,
    path: Optional[str] = None,
) -> bool:
    """Whether ``direction`` is currently latched as terminal.

    A latch older than ``max_age_ms`` (when > 0) is treated as expired so a
    stale lock can't block forever. Fail-open (False) on any uncertainty."""
    target = path or STATE_FILE
    try:
        state = _load(target)
        rec = state["latches"].get(_latch_key(str(coin)[:32], direction))
        if not isinstance(rec, dict):
            return False
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        since = int(rec.get("since_ms", now_ms))
        if max_age_ms > 0 and now_ms - since > max_age_ms:
            return False
        return True
    except Exception:
        return False


def release_if_cooled(
    *,
    coin: str,
    direction: str,
    cooldown_ms: float,
    now_ms: Optional[int] = None,
    path: Optional[str] = None,
) -> bool:
    """Clear a time-based latch once ``cooldown_ms`` has elapsed since it was
    set. Returns True if it was released (or was absent)."""
    target = path or STATE_FILE
    try:
        state = _load(target)
        key = _latch_key(str(coin)[:32], direction)
        rec = state["latches"].get(key)
        if not isinstance(rec, dict):
            return True
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        since = int(rec.get("since_ms", now_ms))
        if cooldown_ms <= 0 or now_ms - since >= cooldown_ms:
            clear_blowoff(coin=coin, direction=direction, path=target)
            return True
        return False
    except Exception:
        return True
