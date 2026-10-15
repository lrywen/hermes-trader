"""Single LLM circuit-breaker state machine shared by every LLM call site.

History (audit 2026-10-15, "safety-net redundancy"): the research brain and the
dashboard chat fallback each carried a hand-copied breaker — separate failure
counters, separate open-until stamps, *different threshold sources* (canonical
config vs. env vars read once at import) and only one side emitted metrics.
That is a **real redundancy** (same predicate, diverging tuning, one side
silent): a dashboard outage never tripped the brain and Prometheus could not
see the chat breaker. This module is the one implementation; each *process*
constructs its own instance with a ``settings_provider`` and optional
``on_trip``/``on_change`` hooks (the brain additionally feeds the cross-process
``ai_brain_ready`` gauge). The processes do not share memory, so per-process
instances are intended — the *logic* is what must not be copied again.

State machine (unchanged from the two prior copies): after ``fail_threshold``
consecutive hard failures the breaker opens for ``cooldown_s`` and
``is_open()`` short-circuits callers to their empty/fallback path; any success
resets the streak; once the cooldown elapses ``is_open()`` goes false and a
success closes it fully (a half-open probe is the caller's normal request).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_COOLDOWN_S = 300.0


class LLMCircuitBreaker:
    """Count consecutive LLM failures and open for a cooldown after N of them.

    ``settings_provider() -> (fail_threshold, cooldown_s)`` is read on every
    failure so hot config changes take effect without a restart. Invalid or
    non-positive settings fall back to the module defaults (fail closed to a
    sane bound, never to "breaker disabled"). ``on_trip`` is invoked once when
    the breaker transitions closed -> open (outside the lock); ``on_reset`` is
    invoked when a success clears a non-zero failure streak.
    """

    def __init__(
        self,
        *,
        name: str = "llm",
        settings_provider: Optional[Callable[[], "tuple[int, float]"]] = None,
        on_trip: Optional[Callable[[int, float], None]] = None,
        on_reset: Optional[Callable[[], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self._settings_provider = settings_provider
        self._on_trip = on_trip
        self._on_reset = on_reset
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    def settings(self) -> "tuple[int, float]":
        threshold: int = DEFAULT_FAIL_THRESHOLD
        cooldown: float = DEFAULT_COOLDOWN_S
        if self._settings_provider is not None:
            try:
                t, c = self._settings_provider()
                threshold = int(t)
                cooldown = float(c)
            except (TypeError, ValueError):
                threshold, cooldown = DEFAULT_FAIL_THRESHOLD, DEFAULT_COOLDOWN_S
        if threshold <= 0:
            threshold = DEFAULT_FAIL_THRESHOLD
        if not (cooldown > 0):
            cooldown = DEFAULT_COOLDOWN_S
        return threshold, cooldown

    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def record_success(self) -> None:
        with self._lock:
            had_streak = self._failures > 0
            self._failures = 0
        if had_streak and self._on_reset is not None:
            try:
                self._on_reset()
            except Exception:
                pass

    def record_failure(self) -> bool:
        """Register a hard failure; returns True if this failure tripped it."""
        threshold, cooldown = self.settings()
        tripped = False
        with self._lock:
            self._failures += 1
            if self._failures >= threshold:
                self._open_until = self._clock() + cooldown
                self._failures = 0
                tripped = True
        if tripped and self._on_trip is not None:
            try:
                self._on_trip(threshold, cooldown)
            except Exception:
                pass
        return tripped
