"""Tests for the setup-age decay factor (roadmap §2).

Covers the pure decay math in indicators.triggers (decay_factor /
composite_score_aged) and the cross-cycle onset tracking + off/shadow/enforce
mode resolution in agents.perception. The full _scan_single_market path needs
network/candles, so it is exercised indirectly: the decision surface here is
the same pure functions that path calls.
"""

from __future__ import annotations

import pytest

from hermes_trader.agents import perception
from hermes_trader.indicators import triggers


def _hit(name: str, score: float = 10.0, fired: bool = True):
    return {"name": name, "score": score, "reason": "t", "fired": fired}


# ── decay_factor (pure math) ────────────────────────────────────────────────
def test_decay_factor_no_decay_cases():
    # No halflife configured (None) → never decay (pulse triggers).
    assert triggers.decay_factor(10_000, None) == 1.0
    # Halflife <= 0 → never decay.
    assert triggers.decay_factor(10_000, 0.0) == 1.0
    assert triggers.decay_factor(10_000, -5.0) == 1.0
    # Age zero (firing on this bar) → full weight.
    assert triggers.decay_factor(0, 1000.0) == 1.0
    # Negative/clock-skew age must never inflate weight above 1.
    assert triggers.decay_factor(-500, 1000.0) == 1.0


def test_decay_factor_halflife_and_asymptote():
    # One halflife of age → factor ~0.5.
    assert triggers.decay_factor(1000.0, 1000.0) == pytest.approx(0.5, abs=1e-9)
    # Two halflives → 0.25.
    assert triggers.decay_factor(2000.0, 1000.0) == pytest.approx(0.25, abs=1e-9)
    # Three half-lives of age → factor 0.5^3 = 0.125.
    assert triggers.decay_factor(3000.0, 1000.0) == pytest.approx(0.125, abs=1e-9)
    # Very old → approaches 0 (never negative).
    f = triggers.decay_factor(1_000_000.0, 1000.0)
    assert 0.0 <= f < 0.01


# ── composite_score_aged (pure aggregation) ─────────────────────────────────
def test_composite_aged_matches_raw_at_age_zero():
    # First-fire bar: onset == current bar → age 0 → identical to composite_score.
    hits = [_hit("breakout", 10.0), _hit("trendStrength", 8.0, fired=False)]
    weights = {"breakout": 0.3, "trendStrength": 0.55}
    bar = 1_000_000
    onset = {"breakout": bar}  # stamped this bar
    hl = {"breakout": 900_000.0}
    assert triggers.composite_score_aged(hits, weights, bar, onset, hl) == pytest.approx(
        triggers.composite_score(hits, weights))


def test_composite_aged_decays_with_age():
    # Only breakout fires (weight 0.3); trendStrength weight 0.55 stays in the
    # (un-decayed) denominator, preserving the surfacing-gate calibration.
    hits = [_hit("breakout", 10.0), _hit("trendStrength", 8.0, fired=False)]
    weights = {"breakout": 0.3, "trendStrength": 0.55}  # total 0.85
    bar = 1_000_000
    hl = {"breakout": 1_000.0}
    raw = triggers.composite_score(hits, weights)
    # Aged one halflife: the breakout contribution is halved.
    aged = triggers.composite_score_aged(hits, weights, bar + 1000, {"breakout": bar}, hl)
    total_weight = sum(weights.values())  # 0.85 (nominal; not decayed)
    expected = (10.0 * weights["breakout"] * 0.5 / total_weight) * 10
    assert aged == pytest.approx(expected, abs=1e-6)
    assert aged == pytest.approx(raw * 0.5, abs=1e-6)
    assert aged < raw


def test_composite_aged_missing_onset_means_new():
    # A fired trigger with no onset entry (new this bar) → full weight.
    hits = [_hit("breakout", 10.0)]
    weights = {"breakout": 0.3}
    assert triggers.composite_score_aged(hits, weights, 5_000, {}, {"breakout": 1000.0}) == \
        pytest.approx(triggers.composite_score(hits, weights))


def test_composite_aged_halflife_zero_never_decays():
    # Pulse trigger (halflife 0) stays at full weight regardless of age.
    hits = [_hit("momentumBurst", 10.0)]
    weights = {"momentumBurst": 0.2}
    bar = 1_000_000
    aged = triggers.composite_score_aged(
        hits, weights, bar + 10_000, {"momentumBurst": bar}, {"momentumBurst": 0.0})
    assert aged == pytest.approx(triggers.composite_score(hits, weights))


def test_composite_aged_no_fired_returns_zero():
    assert triggers.composite_score_aged(
        [_hit("breakout", fired=False)], {"breakout": 0.3}, 1, {}, {}) == 0


# ── onset tracking (perception, thread-safe module state) ───────────────────
@pytest.fixture(autouse=True)
def _reset_onset():
    perception._reset_age_decay()
    yield
    perception._reset_age_decay()


def test_observe_stamps_first_fire_then_ages():
    hl = perception._age_decay_halflives_ms({})
    ttl = 21_600_000
    bar0 = 1_000_000
    # First fire: onset stamped at bar0.
    o1 = perception._age_decay_observe("COIN", ["breakout"], bar0, hl, ttl)
    assert o1["breakout"] == bar0
    # Still firing on a later bar: onset stays at bar0 (age grows).
    bar1 = bar0 + 5 * 60_000  # +5 min
    o2 = perception._age_decay_observe("COIN", ["breakout"], bar1, hl, ttl)
    assert o2["breakout"] == bar0
    assert bar1 - o2["breakout"] == 5 * 60_000


def test_observe_clears_when_setup_goes_quiet_then_restamps():
    hl = perception._age_decay_halflives_ms({})
    ttl = 21_600_000
    bar0 = 1_000_000
    perception._age_decay_observe("COIN", ["breakout"], bar0, hl, ttl)
    # Trigger stops firing → its onset is cleared.
    perception._age_decay_observe("COIN", [], bar0 + 60_000, hl, ttl)
    # Re-fires later → fresh onset (new setup, age resets).
    bar2 = bar0 + 30 * 60_000
    o = perception._age_decay_observe("COIN", ["breakout"], bar2, hl, ttl)
    assert o["breakout"] == bar2


def test_observe_expires_past_ttl():
    hl = perception._age_decay_halflives_ms({})
    ttl = 10 * 60_000  # 10 min
    bar0 = 1_000_000
    perception._age_decay_observe("COIN", ["breakout"], bar0, hl, ttl)
    # A scan gap longer than the TTL: the stale onset is pruned and re-stamped.
    bar_later = bar0 + ttl + 60_000
    o = perception._age_decay_observe("COIN", ["breakout"], bar_later, hl, ttl)
    assert o["breakout"] == bar_later


def test_observe_isolates_coins():
    hl = perception._age_decay_halflives_ms({})
    ttl = 21_600_000
    bar0 = 1_000_000
    perception._age_decay_observe("AAA", ["breakout"], bar0, hl, ttl)
    o = perception._age_decay_observe("BBB", ["breakout"], bar0 + 60_000, hl, ttl)
    # BBB sees its own fresh onset; AAA's onset does not leak.
    assert o["breakout"] == bar0 + 60_000


def test_observe_skips_triggers_without_halflife_config():
    # Only triggers present in the halflife map are returned for scoring.
    hl = {"breakout": 900_000.0}
    o = perception._age_decay_observe("COIN", ["breakout", "someOther"], 1_000, hl, 99_999)
    assert "breakout" in o
    assert "someOther" not in o


# ── config resolution ───────────────────────────────────────────────────────
def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv("HERMES_SIGNAL_AGE_DECAY_MODE", raising=False)
    assert perception._age_decay_config({})["mode"] == "off"
    assert perception._age_decay_config({"signal_age_decay": {}})["mode"] == "off"


def test_config_mode_from_block(monkeypatch):
    monkeypatch.delenv("HERMES_SIGNAL_AGE_DECAY_MODE", raising=False)
    assert perception._age_decay_config(
        {"signal_age_decay": {"mode": "shadow"}})["mode"] == "shadow"
    assert perception._age_decay_config(
        {"signal_age_decay": {"mode": "ENFORCE"}})["mode"] == "enforce"


def test_config_env_overrides_block(monkeypatch):
    monkeypatch.setenv("HERMES_SIGNAL_AGE_DECAY_MODE", "enforce")
    assert perception._age_decay_config(
        {"signal_age_decay": {"mode": "shadow"}})["mode"] == "enforce"


def test_config_invalid_mode_falls_back_off(monkeypatch):
    monkeypatch.setenv("HERMES_SIGNAL_AGE_DECAY_MODE", "bogus")
    assert perception._age_decay_config({})["mode"] == "off"


def test_halflives_defaults_pulse_never_decay():
    hl = perception._age_decay_halflives_ms({})
    # Pulse triggers default to 0 (never decay); formation triggers positive.
    assert hl["momentumBurst"] == 0.0
    assert hl["pctMoveSpike"] == 0.0
    assert hl["volumeSpike"] == 0.0
    assert hl["breakout"] == 900.0 * 1000
    assert hl["trendStrength"] == 1800.0 * 1000
    assert hl["higherLows1h"] == 7200.0 * 1000


def test_halflives_override_and_bad_value(monkeypatch):
    monkeypatch.delenv("HERMES_SIGNAL_AGE_DECAY_MODE", raising=False)
    blk = {"halflife_s": {"breakout": 120.0, "trendStrength": "junk"}}
    hl = perception._age_decay_halflives_ms(blk)
    assert hl["breakout"] == 120.0 * 1000
    # Non-numeric override falls back to the default.
    assert hl["trendStrength"] == 1800.0 * 1000
