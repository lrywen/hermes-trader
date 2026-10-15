"""Tests for the P0 (audit 2026-10-15) safety-net de-duplication:

  * P0-1: confidence-threshold single-source guard + runner resolver;
  * P0-2: the shared LLM circuit-breaker state machine used by both the
    research brain and the dashboard chat fallback.
"""
from __future__ import annotations

import threading

from hermes_trader.agents.confidence_guard import (
    confidence_threshold_warnings,
    resolve_runner_generic_confidence,
    runner_generic_confidence,
)
from hermes_trader.agents.llm_circuit_breaker import LLMCircuitBreaker

# ── P0-1: confidence single source ─────────────────────────────────────────

def test_aligned_thresholds_produce_no_warning():
    cfg = {"min_ai_confidence": 0.60,
           "runner_entry_gate": {"min_confidence": 0.60,
                                  "min_short_confidence": 0.72,
                                  "mover_min_confidence": 0.72}}
    assert confidence_threshold_warnings(cfg) == []


def test_drifted_nested_key_is_flagged():
    cfg = {"min_ai_confidence": 0.60,
           "runner_entry_gate": {"min_confidence": 0.70}}
    w = confidence_threshold_warnings(cfg)
    assert len(w) == 1
    assert "0.60" in w[0] and "0.70" in w[0] and "drift" in w[0]


def test_resolver_derives_from_canonical_when_nested_absent():
    cfg = {"min_ai_confidence": 0.60, "runner_entry_gate": {}}
    assert runner_generic_confidence(cfg) is None
    assert resolve_runner_generic_confidence(cfg) == 0.60


def test_resolver_honours_explicit_nested_for_emergency_override():
    cfg = {"min_ai_confidence": 0.60,
           "runner_entry_gate": {"min_confidence": 0.80}}
    assert resolve_runner_generic_confidence(cfg) == 0.80


def test_resolver_falls_back_when_everything_missing():
    assert resolve_runner_generic_confidence({}) == 0.70


def test_situation_bar_below_generic_is_flagged():
    cfg = {"min_ai_confidence": 0.60,
           "runner_entry_gate": {"min_confidence": 0.60,
                                  "mover_min_confidence": 0.50}}
    w = confidence_threshold_warnings(cfg)
    assert any("mover_min_confidence" in x for x in w)


def test_missing_canonical_reports_and_does_not_raise():
    w = confidence_threshold_warnings({"runner_entry_gate": {}})
    assert len(w) == 1 and "min_ai_confidence" in w[0]


def test_canonical_defaults_are_aligned():
    # The two generic floors must ship equal in canonical defaults; this is the
    # regression lock that the 0.60 vs 0.70 split can never silently return.
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS
    assert float(CANONICAL_DEFAULTS["min_ai_confidence"]) == float(
        CANONICAL_DEFAULTS["runner_entry_gate"]["min_confidence"])


# ── P0-2: shared LLM circuit breaker ───────────────────────────────────────

def _fake_clock():
    cur = [1000.0]
    lock = threading.Lock()

    def now():
        with lock:
            return cur[0]

    def advance(seconds):
        with lock:
            cur[0] += seconds
    return now, advance


def test_breaker_opens_after_threshold_failures():
    now, advance = _fake_clock()
    trips = []
    cb = LLMCircuitBreaker(
        settings_provider=lambda: (3, 300.0),
        on_trip=lambda t, c: trips.append((t, c)),
        clock=now)
    assert not cb.is_open()
    assert cb.record_failure() is False
    assert cb.record_failure() is False
    assert cb.is_open() is False
    assert cb.record_failure() is True           # third failure trips
    assert cb.is_open()
    assert trips == [(3, 300.0)]
    advance(299)
    assert cb.is_open()                          # still within cooldown
    advance(2)
    assert not cb.is_open()                      # half-open after cooldown


def test_success_resets_streak_before_trip():
    now, _ = _fake_clock()
    cb = LLMCircuitBreaker(settings_provider=lambda: (3, 300.0), clock=now)
    cb.record_failure(); cb.record_failure()
    reset = []
    cb._on_reset = lambda: reset.append(1)
    cb.record_success()
    cb.record_failure(); cb.record_failure()
    assert not cb.is_open()                      # streak restarted at 0
    assert reset == [1]


def test_settings_read_dynamically_on_each_failure():
    now, _ = _fake_clock()
    setting = {"v": 2}
    cb = LLMCircuitBreaker(
        settings_provider=lambda: (setting["v"], 60.0), clock=now)
    cb.record_failure()
    assert cb.record_failure() is True           # threshold 2 trips
    setting["v"] = 5


def test_invalid_settings_fall_back_to_defaults():
    now, _ = _fake_clock()
    cb = LLMCircuitBreaker(settings_provider=lambda: (0, -5), clock=now)
    threshold, cooldown = cb.settings()
    assert threshold == 3 and cooldown == 300.0


def test_default_settings_when_no_provider():
    now, _ = _fake_clock()
    cb = LLMCircuitBreaker(clock=now)
    threshold, cooldown = cb.settings()
    assert threshold == 3 and cooldown == 300.0
    for _ in range(2):
        assert cb.record_failure() is False
    assert cb.record_failure() is True


def test_on_trip_exception_does_not_break_recording():
    now, _ = _fake_clock()

    def boom(t, c):
        raise RuntimeError("metrics down")

    cb = LLMCircuitBreaker(
        settings_provider=lambda: (1, 10.0), on_trip=boom, clock=now)
    assert cb.record_failure() is True           # hook raised but state set
    assert cb.is_open()


def test_research_and_dashboard_share_one_class():
    import hermes_trader.dashboard as dashboard
    from hermes_trader.agents import research
    assert isinstance(research._llm_breaker, LLMCircuitBreaker)
    assert isinstance(dashboard._llm_breaker, LLMCircuitBreaker)
    # separate per-process state instances, same state machine
    assert research._llm_breaker is not dashboard._llm_breaker
