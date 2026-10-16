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
from hermes_trader.agents.exit_param_guard import (
    exit_shadow_findings,
    exit_shadow_warnings,
    stop_width_drift_findings,
    stop_width_drift_warnings,
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
    # P3 (2026-10-15): the built-in last-resort fallback now mirrors
    # CANONICAL_DEFAULTS["min_ai_confidence"] (0.60), not the drifted 0.70.
    from hermes_trader.agents.confidence_guard import DEFAULT_MIN_AI_CONFIDENCE

    assert resolve_runner_generic_confidence({}) == DEFAULT_MIN_AI_CONFIDENCE == 0.60


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


# ── P0-3: regime-aware exit dual-track guard ───────────────────────────────

# A config shaped like production: regime_aware enabled, both max_loss buckets
# and both clock buckets populated, trend_ride directional only.
_FULL_CFG = {
    "dsl_exit": {
        "protect_pct": 1.25,
        "retrace_threshold": 0.2,
        "max_loss_pct": 1.5,
        "max_loss_roe_pct": 1000.0,
        "hard_timeout_minutes": 1800.0,
        "stale_flat_timeout_minutes": 480,
        "regime_aware": {
            "enabled": True,
            "trend_ride": {"protect_pct": 3.0, "retrace_threshold": 0.55},
            "max_loss": {
                "trend": {"max_loss_pct": 1.5, "max_loss_roe_pct": 1000.0},
                "non_trend": {"max_loss_pct": 1.5, "max_loss_roe_pct": 1000.0},
            },
            "clocks": {
                "enabled": True,
                "trend": {"hard_timeout_minutes": 240.0,
                          "stale_flat_timeout_minutes": 120.0},
                "non_trend": {"hard_timeout_minutes": 120.0,
                              "stale_flat_timeout_minutes": 60.0},
            },
        },
    }
}


def _finding_map(cfg):
    return {f["key"]: f for f in exit_shadow_findings(cfg)}


def test_disabled_tracks_report_nothing():
    cfg = {"dsl_exit": {"max_loss_pct": 1.0,
                        "regime_aware": {"enabled": False}}}
    assert exit_shadow_findings(cfg) == []


def test_missing_dsl_exit_is_silent():
    assert exit_shadow_findings({}) == []


def test_protect_shadowed_only_in_directional_regimes():
    m = _finding_map(_FULL_CFG)
    f = m["dsl_exit.protect_pct"]
    assert f["shadowed_in"] == "up, down"
    assert f["reachable_in"] == "neutral, chop"


def test_max_loss_fully_shadowed_when_both_buckets_present():
    m = _finding_map(_FULL_CFG)
    for key in ("dsl_exit.max_loss_pct", "dsl_exit.max_loss_roe_pct"):
        assert m[key]["shadowed_in"] == "up, down, neutral, chop"
        assert m[key]["reachable_in"] == "nowhere"


def test_clocks_fully_shadowed_when_both_buckets_present():
    m = _finding_map(_FULL_CFG)
    for key in ("dsl_exit.hard_timeout_minutes",
                "dsl_exit.stale_flat_timeout_minutes"):
        assert m[key]["shadowed_in"] == "up, down, neutral, chop"
        assert m[key]["reachable_in"] == "nowhere"


def test_nontrend_bucket_absent_keeps_top_level_reachable_in_chop():
    cfg = {
        "dsl_exit": {
            "max_loss_pct": 1.0,
            "regime_aware": {
                "enabled": True,
                "max_loss": {"trend": {"max_loss_pct": 4.0}},
            },
        }
    }
    f = _finding_map(cfg)["dsl_exit.max_loss_pct"]
    assert f["shadowed_in"] == "up, down"
    assert f["reachable_in"] == "neutral, chop"


def test_clocks_disabled_leaves_clock_keys_live():
    cfg = {
        "dsl_exit": {
            "hard_timeout_minutes": 240,
            "stale_flat_timeout_minutes": 90,
            "regime_aware": {"enabled": True,
                             "trend_ride": {"protect_pct": 2.5},
                             "clocks": {"enabled": False}},
        }
    }
    m = _finding_map(cfg)
    assert "dsl_exit.hard_timeout_minutes" not in m
    assert "dsl_exit.stale_flat_timeout_minutes" not in m
    assert "dsl_exit.protect_pct" in m  # regime_aware still active


def test_warnings_are_human_readable():
    msgs = exit_shadow_warnings(_FULL_CFG)
    assert any("dsl_exit.max_loss_pct" in m and "SHADOWED" in m for m in msgs)
    assert all(m.startswith("exit dual-track:") for m in msgs)


# ── P1 (SL width): backup-stop-width drift guard ───────────────────────────


def test_stop_width_aligned_shared_block_is_silent():
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 1.2, "floor_pct": 1.2}},
           "sl_atr_mult": 1.2, "sl_floor_pct": 1.2}
    assert stop_width_drift_findings(cfg) == []


def test_stop_width_divergent_override_is_flagged():
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 1.2, "floor_pct": 1.2}},
           "sl_atr_mult": 1.5}
    f = stop_width_drift_findings(cfg)
    assert len(f) == 1
    assert f[0]["top_key"] == "sl_atr_mult" and f[0]["top_value"] == 1.5
    assert f[0]["shared_value"] == 1.2


def test_stop_width_absent_override_is_silent():
    # no top-level sl_* -> shared block is the single source (desired state)
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 1.2, "floor_pct": 1.2}}}
    assert stop_width_drift_findings(cfg) == []


def test_stop_width_floor_drift_is_flagged_separately():
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 1.2, "floor_pct": 1.2}},
           "sl_floor_pct": 1.5}
    f = stop_width_drift_findings(cfg)
    assert len(f) == 1 and f[0]["top_key"] == "sl_floor_pct"


def test_stop_width_ceiling_divergence_not_flagged():
    # backup/DSL ceilings are intentionally different -> never warned
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 1.2, "floor_pct": 1.2,
                                     "ceiling_pct": 3.0}},
           "sl_ceiling_pct": 3.0}
    assert stop_width_drift_findings(cfg) == []


def test_stop_width_warnings_text():
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 1.2, "floor_pct": 1.2}},
           "sl_atr_mult": 1.5}
    msgs = stop_width_drift_warnings(cfg)
    assert len(msgs) == 1
    assert "sl_atr_mult=1.5" in msgs[0] and "atr_mult=1.2" in msgs[0]
    assert "single source" in msgs[0]


def test_stop_width_missing_shared_block_is_silent():
    cfg = {"sl_atr_mult": 1.5}
    assert stop_width_drift_findings(cfg) == []
