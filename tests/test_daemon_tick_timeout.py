"""Tests for the daemon tick-timeout SIGALRM arm/disarm logic.

These tests exercise the real ``signal.alarm`` mechanism, so they must run on
the main thread (pytest default) and are Unix-only. Every test disarms in
teardown so a pending alarm can never leak into the next test (``signal.alarm``
is a process-global, one-shot timer).

Scope: these verify the *primitives* ``_arm_tick_alarm`` / ``_disarm_tick_alarm``
behave correctly in isolation. The producer_daemon loop bug (arm once outside
the loop, then disarm every tick without re-arming) is a *composition* issue —
these tests pin the building blocks so the loop fix can rely on them.
"""

import signal
import time

import pytest

from hermes_trader.client.daemon import (
    _TickTimeout,
    _arm_tick_alarm,
    _disarm_tick_alarm,
)

# _arm_tick_alarm uses int(seconds) + 1, so the minimum real budget is 1s.
# To reliably observe a fire we sleep past that budget.
_MIN_BUDGET_S = 1
_SLEEP_PAST_BUDGET_S = 2.5
# A long budget that must NOT fire during a short sleep.
_LONG_BUDGET_S = 30


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM is Unix-only")
class TestTickTimeoutAlarm:
    def teardown_method(self) -> None:
        # Never leave a pending alarm between tests.
        _disarm_tick_alarm()

    def test_arm_returns_true_on_unix(self) -> None:
        assert _arm_tick_alarm(_LONG_BUDGET_S) is True

    def test_arm_schedules_alarm_that_fires(self) -> None:
        """An armed alarm must raise _TickTimeout once the budget elapses."""
        _arm_tick_alarm(0)  # int(0)+1 => 1s budget
        start = time.monotonic()
        with pytest.raises(_TickTimeout):
            time.sleep(_SLEEP_PAST_BUDGET_S)
        elapsed = time.monotonic() - start
        # Fired around the 1s mark, well before we finished sleeping.
        assert elapsed < _SLEEP_PAST_BUDGET_S

    def test_arm_returns_false_without_sigalrm(self, monkeypatch) -> None:
        """On a platform without SIGALRM, arm must report False and not crash."""
        monkeypatch.delattr(signal, "SIGALRM", raising=False)
        assert _arm_tick_alarm(_LONG_BUDGET_S) is False

    def test_disarm_cancels_pending_alarm(self) -> None:
        """Disarming must cancel the pending alarm so nothing fires."""
        _arm_tick_alarm(0)  # would fire at ~1s
        _disarm_tick_alarm()
        time.sleep(_SLEEP_PAST_BUDGET_S)  # safely past the would-be fire time
        # Reaching here without _TickTimeout proves disarm worked.

    def test_arm_after_disarm_rearms(self) -> None:
        """arm -> disarm -> arm must re-establish a working alarm.

        This pins the exact sequence the producer_daemon loop fix relies on:
        re-arming after a disarm gives back the per-tick timeout protection
        instead of leaving the tick unguarded.
        """
        _arm_tick_alarm(_LONG_BUDGET_S)
        _disarm_tick_alarm()
        _arm_tick_alarm(0)  # re-arm a short budget
        with pytest.raises(_TickTimeout):
            time.sleep(_SLEEP_PAST_BUDGET_S)

    def test_disarm_is_idempotent(self) -> None:
        """Calling disarm when no alarm is pending must not raise."""
        _disarm_tick_alarm()
        _disarm_tick_alarm()