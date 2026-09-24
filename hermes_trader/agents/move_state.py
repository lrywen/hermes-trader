"""Per-coin directional-move state machine (anti-late-chase, 2026-09-22).

Why this exists
---------------
BCH 2026-09-22: the 12:30 UTC vertical launch (270→321, +19% in 43 min) was
missed at its origin because the coin only entered the Top-N scan list once it
had already rallied. The system then re-researched it five times; the first
verdict correctly said PASS ("do not chase"), but PASS had no memory, so the
book kept re-evaluating the same move and eventually bought the terminal tick
at 13:13 — stopped for -6% ROE four minutes later.

The fix is move-level memory: once a coin begins a strong directional move we
record its anchor (origin price/time). Entry is only allowed while the price is
still within a ``fresh_max_extension_pct`` band of the anchor. Beyond that band
the move is "missed"; the state locks further entry in the SAME move until the
coin returns to a fresh anchor (a pullback / a new base), so a parabolic
extension can never be chased merely because its score keeps rising with price.

State semantics
---------------
* A move is keyed per coin per UTC day; it resets at the day roll and when the
  price retraces back inside the anchor band (the extension "resets").
* ``observe`` is the single writer, called by the trading loop with the current
  mid and whether a fresh directional impulse is present. Best-effort, never
  raises.
* ``entry_allowed`` answers "is this price still in the fresh window of the
  current move?"; a coin with no active move (calm / range) is always allowed —
  this module only ever tightens a hot, already-extended move.

Contract: atomic rewrite (no torn reads), no fsync; missing/corrupt state
degrades to "allowed" (fail-open) so a state-file fault can never stall the
order path.
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
    "HERMES_MOVE_STATE_FILE", "/data/.move-state.json"
)
_STATE_VERSION = 1


def _utc_day(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("moves"), dict):
            return data
    except (FileNotFoundError, ValueError, OSError, TypeError):
        pass
    return {"version": _STATE_VERSION, "moves": {}}


def observe(
    *,
    coin: str,
    mid: float,
    impulse: bool,
    impulse_dir: str = "",
    fresh_max_extension_pct: float = 8.0,
    reset_retrace_fraction: float = 0.5,
    min_anchor_age_sec: float = 300.0,
    min_move_extension_pct_for_reset: float = 3.0,
    path: Optional[str] = None,
) -> None:
    """Update the move state for one coin from the current scan.

    ``impulse`` / ``impulse_dir`` describe a fresh directional trigger
    (breakout / momentum burst). The first impulse of a day (or after a reset)
    anchors the move at ``mid``. While price stays within
    ``fresh_max_extension_pct`` of the anchor the move is "fresh"; once it runs
    beyond that band it becomes "missed" (locked). A pullback of at least
    ``reset_retrace_fraction`` of the extension back toward the anchor clears
    the lock so a genuine pullback/re-base can be traded again.

    P2 re-anchor guards (the late-chase memory was being erased by noise):
    * ``min_anchor_age_sec`` — an existing anchor younger than this is not
      replaced by a fresh opposite/secondary impulse, so a terminal vertical
      30 s after the origin cannot re-paint the anchor to "now".
    * ``min_move_extension_pct_for_reset`` — a missed move only resets after it
      actually ran this far and then genuinely retraced; shallow wiggle on a
      move that never developed cannot clear the lock and re-anchor.

    Best-effort: any error is debug-logged and never propagates.
    """
    target = path or STATE_FILE
    try:
        coin = str(coin or "")[:32]
        price = float(mid or 0.0)
        if not coin or price <= 0:
            return
        band = float(fresh_max_extension_pct or 0.0)
        state = _load(target)
        moves = state["moves"]
        day = _utc_day()
        rec = moves.get(coin)
        now_ms = int(time.time() * 1000)
        if rec is None or rec.get("day") != day:
            # No move today yet. Only anchor on a directional impulse; a calm
            # coin needs no record (entry stays allowed by default).
            if impulse and impulse_dir in ("up", "down"):
                moves[coin] = {
                    "day": day,
                    "dir": impulse_dir,
                    "anchor": price,
                    "anchor_ts": now_ms,
                    "missed": False,
                    "extreme": price,
                    "updated_ts": now_ms,
                }
            else:
                moves.pop(coin, None)
        else:
            direction = str(rec.get("dir") or "")
            anchor = float(rec.get("anchor") or price)
            # Signed extension in the move's direction (% from anchor).
            if direction == "up":
                ext = (price - anchor) / anchor * 100.0 if anchor > 0 else 0.0
                extreme = max(float(rec.get("extreme") or price), price)
            else:
                ext = (anchor - price) / anchor * 100.0 if anchor > 0 else 0.0
                extreme = min(float(rec.get("extreme") or price), price)
            max_ext = (extreme - anchor) / anchor * 100.0 if direction == "up" \
                else (anchor - extreme) / anchor * 100.0

            missed = bool(rec.get("missed"))
            if not missed and band > 0 and ext > band:
                missed = True
            elif missed:
                # Reset when price has retraced at least reset_retrace_fraction
                # of the achieved extension back toward the anchor.
                retrace = max_ext - ext
                # P2: only a move that actually developed can reset, and it must
                # genuinely retrace from its extreme — a shallow wiggle near the
                # top keeps the lock instead of re-anchoring at "now".
                if (max_ext >= float(min_move_extension_pct_for_reset)
                        and max_ext > 0
                        and retrace >= max_ext * float(reset_retrace_fraction or 0.5)):
                    # Re-anchor at current price (a fresh base after pullback).
                    rec["anchor"] = price
                    rec["anchor_ts"] = now_ms
                    rec["extreme"] = price
                    missed = False

            # A fresh impulse pointing AGAINST the current move starts a new
            # (opposite) move — but only once the current anchor has aged past
            # min_anchor_age_sec. P2: without this a terminal impulse 30 s after
            # the origin (same fast vertical, trigger flickering) would re-paint
            # the anchor to the current price and erase the late-chase memory.
            anchor_age_sec = (now_ms - int(rec.get("anchor_ts") or now_ms)) / 1000.0
            if (impulse and impulse_dir in ("up", "down")
                    and impulse_dir != direction
                    and anchor_age_sec >= float(min_anchor_age_sec)):
                rec["dir"] = impulse_dir
                rec["anchor"] = price
                rec["anchor_ts"] = now_ms
                rec["extreme"] = price
                missed = False

            rec["extreme"] = extreme
            rec["missed"] = missed
            rec["updated_ts"] = now_ms
            moves[coin] = rec

        state["moves"] = moves
        write_json_atomic(target, state, indent=None, fsync=False)
    except Exception as e:  # never perturb the trading loop
        logger.debug("[move_state] observe failed for %s: %s", coin, e)


def get_move(coin: str, path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the current move record for a coin, or None.

    Stale-day records (from a previous UTC day) are treated as no move.
    """
    target = path or STATE_FILE
    try:
        state = _load(target)
        rec = state["moves"].get(str(coin or "")[:32])
        if not isinstance(rec, dict):
            return None
        if rec.get("day") != _utc_day():
            return None
        return rec
    except Exception:
        return None


def entry_allowed(
    *,
    coin: str,
    side: str,
    mid: float,
    fresh_max_extension_pct: float = 8.0,
    path: Optional[str] = None,
) -> tuple[bool, str]:
    """Decide whether entering ``side`` at ``mid`` is still in the fresh window.

    Returns ``(allowed, reason)``. Fail-open on any uncertainty. Only blocks a
    trade that joins an active, already-missed (over-extended) move in the same
    direction.
    """
    try:
        band = float(fresh_max_extension_pct or 0.0)
        if band <= 0:
            return True, ""
        price = float(mid or 0.0)
        rec = get_move(coin, path)
        if rec is None or price <= 0:
            return True, ""
        direction = str(rec.get("dir") or "")
        joining = (side == "long" and direction == "up") or \
                  (side == "short" and direction == "down")
        if not joining:
            return True, ""
        anchor = float(rec.get("anchor") or price)
        ext = abs((price - anchor) / anchor * 100.0) if anchor > 0 else 0.0
        if rec.get("missed") or ext > band:
            return False, (
                f"missed move: {side} {ext:.1f}% from {direction} anchor "
                f"(fresh band {band:.1f}%) — no late chase")
        return True, ""
    except Exception:
        return True, ""
