"""Phase-4 realtime feed policy helpers (P0-1 dynamic cadence, P0-3 ws_status).

Pure decision logic, deliberately kept OUT of ``scripts/trading_loop.py``
(which runs a module-level ``while True`` and therefore cannot be imported)
so the dynamic scan cadence and the ws_status edge/hysteresis state machine
are unit-testable across the normal / degraded / failed paths.

No I/O lives here: the trading loop passes in the WS/REST feed ages and
enacts the returned decision. Every env switch defaults OFF in the loop so
that, unless explicitly enabled, Phase-1 behaviour (fixed 15s cadence, no
ws_status events) is preserved bit-for-bit.
"""

from __future__ import annotations

import time
from typing import Optional

# Feed states, ordered best → worst. The ws_status SSE event carries these
# verbatim; the Portal indicator maps ok→green, degraded→yellow, down→red.
FEED_OK = "ok"
FEED_DEGRADED = "degraded"
FEED_DOWN = "down"
_RANK = {FEED_OK: 2, FEED_DEGRADED: 1, FEED_DOWN: 0}
_VALID = set(_RANK)


def dynamic_scan_interval(
    base_s: int,
    *,
    ws_age_s: Optional[float],
    dynamic_on: bool,
    fresh_s: float = 10.0,
    fast_s: int = 8,
    slow_s: int = 20,
) -> int:
    """Effective scan cadence (seconds) for the current feed state.

    * ``dynamic_on=False`` → always ``base_s`` (Phase-1 fixed cadence; this is
      the default — HERMES_SCAN_DYNAMIC unset/off).
    * ``dynamic_on=True`` and the WS allMids feed is fresh
      (``ws_age_s`` not None and younger than ``fresh_s``) → ``fast_s``
      (default 8s): positions / equity / close-perception latency shrinks.
    * WS stopped or stale (REST fallback) → ``slow_s`` (default 20s): fewer
      REST tokens are spent while the feed is degraded.

    Pure function: no env reads, no I/O, never raises on bad input.
    """
    if not dynamic_on:
        return int(base_s)
    try:
        age = float(ws_age_s) if ws_age_s is not None else None
    except (TypeError, ValueError):
        age = None
    if age is not None and age < float(fresh_s):
        return int(fast_s)
    return int(slow_s)


def classify_feed_status(
    *,
    ws_age_s: Optional[float],
    rest_age_s: Optional[float],
    ws_fresh_s: float = 10.0,
    rest_fresh_s: float = 30.0,
) -> str:
    """Classify the combined feed liveness into ok / degraded / down.

    * WS allMids frame younger than ``ws_fresh_s`` → ``ok`` (realtime feed).
    * WS not fresh but the REST main-book mids fetch is younger than
      ``rest_fresh_s`` → ``degraded`` (REST fallback, trading continues).
    * Neither feed fresh (or neither has produced data yet past the budgets)
      → ``down`` — the A-F14 gate in the trading loop fail-closes entries and
      DSL market-close decisions; this classifier only surfaces the state.

    ``None`` ages mean "no data point" and count as NOT fresh for that
    source, matching mid_feed_age_seconds()'s cold-start semantics.
    """
    ws_fresh = (
        ws_age_s is not None
        and _safe_float(ws_age_s) is not None
        and _safe_float(ws_age_s) < float(ws_fresh_s)
    )
    if ws_fresh:
        return FEED_OK
    rest_fresh = (
        rest_age_s is not None
        and _safe_float(rest_age_s) is not None
        and _safe_float(rest_age_s) < float(rest_fresh_s)
    )
    if rest_fresh:
        return FEED_DEGRADED
    return FEED_DOWN


def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class FeedStatusTracker:
    """Edge-triggered feed-status state machine with downgrade hysteresis.

    Fed one classification per scan cycle via :meth:`evaluate`. Emits a
    transition descriptor ONLY on a state change:

    * The first evaluation after construction establishes a SILENT baseline
      (no event) so a normal startup — WS connecting for a second or two —
      does not fire a false "degraded/down" alarm.
    * Upgrades (degraded/down → ok, down → degraded) commit IMMEDIATELY: the
      operator sees recovery without waiting.
    * Downgrades (ok → degraded/down, degraded → down) must PERSIST for
      ``hold_seconds`` before committing, so a sub-hold WS blip never flaps
      the Portal indicator or the Feishu/voice alerts. The A-F14 trading gate
      is independent and still fail-closes instantly; hysteresis only debounces
      the *notification*.

    Pure logic (``now`` injectable for deterministic tests); no I/O.
    """

    def __init__(self, *, hold_seconds: float = 30.0) -> None:
        self._state: str = "unknown"
        self._candidate: Optional[str] = None
        self._candidate_since: float = 0.0
        self._hold: float = float(hold_seconds)

    @property
    def state(self) -> str:
        return self._state

    def evaluate(
        self, status: str, now: Optional[float] = None
    ) -> Optional[dict]:
        """Process one classification; return a transition dict or None.

        Transition dict keys: ``status`` (new), ``previous`` (old),
        ``reason`` ("recovered" for upgrades, "degraded"/"down" for a
        hysteresis-confirmed downgrade).
        """
        if now is None:
            now = time.time()
        if status not in _VALID:
            return None
        # Silent baseline: first classification just seeds the state.
        if self._state == "unknown":
            self._state = status
            self._candidate = None
            return None
        if status == self._state:
            # Back to the committed state — cancel any pending downgrade.
            self._candidate = None
            return None
        if _RANK[status] > _RANK[self._state]:
            # Upgrade: commit immediately.
            return self._commit(status, reason="recovered")
        # Downgrade: require the candidate to persist for the hold window.
        if self._candidate != status:
            self._candidate = status
            self._candidate_since = float(now)
            return None
        if (float(now) - self._candidate_since) >= self._hold:
            return self._commit(
                status,
                reason="down" if status == FEED_DOWN else "degraded",
            )
        return None

    def _commit(self, status: str, *, reason: str) -> dict:
        previous = self._state
        self._state = status
        self._candidate = None
        return {"status": status, "previous": previous, "reason": reason}
