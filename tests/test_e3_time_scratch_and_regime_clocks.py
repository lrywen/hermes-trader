"""E3 (Audit 2026-09-06, P2 choppy-market) — exit-side time features.

Two pieces, BOTH inert by default:

* Part B — ``time_scratch`` (time take-profit / scratch exit): a position that
  has been held N minutes, NEVER armed phase-2 (peak < protect_pct), DID show a
  small favorable pop (peak >= min_peak_pct) but has since given most of it back
  while still green, is closed at market. This banks the small win / scratches
  before the trade decays into a stale-flat loser — a gap the existing clocks
  leave open (stale_flat cuts at 8h regardless of PnL; there was no early
  "take-it-and-run" channel). Default OFF.

* Part A — regime-split lifetime clocks: ``regime_aware.clocks`` lets trend
  regimes (up/down) get LONGER hard/stale clocks and non-trend (neutral/chop)
  SHORTER ones, instead of one global clock for every trade. Resolved by
  ``executor.resolve_regime_clocks``; gated behind ``clocks.enabled`` so the
  default config keeps the single global clock (inert).
"""
from __future__ import annotations

import time

import pytest

from hermes_trader.agents.dsl_exit import (
    DSLTracker,
    ExitPolicy,
    _tracker_from_dict,
    _tracker_to_dict,
)


def _scratch_policy(**kw):
    base = dict(
        max_loss_pct=3.5, max_loss_roe_pct=100.0, protect_pct=1.0,
        retrace_threshold=0.40, hard_timeout_minutes=99999.0,
        stale_flat_timeout_minutes=0.0, hard_stop_confirm_sec=0.0,
        breach_confirm_sec=0.0,
        time_scratch_enabled=True, time_scratch_minutes=60.0,
        time_scratch_min_peak_pct=0.3, time_scratch_giveback_pct=0.3,
    )
    base.update(kw)
    return ExitPolicy(**base)


def _tracker(pol, age_min, monkeypatch, coin="E3S", entry=100.0):
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(dsl_exit, "_request_save", lambda **_k: None)
    return DSLTracker(coin, "long", entry, time.time() - age_min * 60,
                      pol, leverage=1)


# ═══════════════════════════════ Part B ════════════════════════════════════

def test_e3_time_scratch_defaults_off():
    pol = ExitPolicy()
    assert pol.time_scratch_enabled is False
    assert pol.time_scratch_minutes == pytest.approx(60.0)
    assert pol.time_scratch_min_peak_pct > 0
    assert pol.time_scratch_giveback_pct > 0


def test_e3_scratch_fires_on_giveback_after_hold(monkeypatch):
    """Aged 90min, peaked +0.6% (never armed, protect=1.0), now +0.2% (gave back
    0.4 >= giveback 0.3, still green) -> scratch out."""
    tr = _tracker(_scratch_policy(), 90, monkeypatch)
    assert tr.check(100.6).exit is False   # peak +0.6%
    v = tr.check(100.2)                    # back to +0.2%
    assert v.exit is True
    assert "time_scratch" in v.reason


def test_e3_scratch_holds_before_min_minutes(monkeypatch):
    """Only 30min in (< 60) -> even with the giveback, do not scratch."""
    tr = _tracker(_scratch_policy(), 30, monkeypatch)
    assert tr.check(100.6).exit is False
    v = tr.check(100.2)
    assert v.exit is False
    assert "time_scratch" not in (v.reason or "")


def test_e3_scratch_spares_armed_phase2(monkeypatch):
    """Once phase-2 has armed (peak >= protect) the trailing floor owns the
    exit; time_scratch must not fire on a winner that pulled back."""
    tr = _tracker(_scratch_policy(), 90, monkeypatch)
    assert tr.check(101.5).exit is False   # peak +1.5% >= protect 1.0 (armed)
    v = tr.check(100.4)                    # gave back to +0.4%
    assert not (v.exit and "time_scratch" in v.reason)


def test_e3_scratch_holds_while_near_peak(monkeypatch):
    """Still +0.5% with peak +0.6% (gave back only 0.1 < 0.3) -> hold."""
    tr = _tracker(_scratch_policy(), 90, monkeypatch)
    assert tr.check(100.6).exit is False
    v = tr.check(100.5)
    assert v.exit is False


def test_e3_scratch_holds_when_peak_too_small(monkeypatch):
    """Never reached min_peak_pct (0.3) -> let stale_flat / max_loss handle it."""
    tr = _tracker(_scratch_policy(), 90, monkeypatch)
    assert tr.check(100.1).exit is False   # peak only +0.1%
    v = tr.check(100.05)
    assert v.exit is False


def test_e3_scratch_disabled_is_inert(monkeypatch):
    """With the flag off the same price path must NOT scratch."""
    tr = _tracker(_scratch_policy(time_scratch_enabled=False), 90, monkeypatch)
    assert tr.check(100.6).exit is False
    v = tr.check(100.2)
    assert v.exit is False


def test_e3_scratch_does_not_mask_a_loss(monkeypatch):
    """time_scratch is a profit-taking channel only: a position that has round-
    tripped INTO a loss must be left to the hard stop, never labelled scratch."""
    tr = _tracker(_scratch_policy(), 90, monkeypatch)
    assert tr.check(100.6).exit is False   # peaked +0.6%
    v = tr.check(99.9)                     # now -0.1% (green? no)
    assert not (v.exit and "time_scratch" in v.reason)


def test_e3_scratch_round_trips_through_state(monkeypatch):
    pol = _scratch_policy()
    tr = DSLTracker("E3RT", "long", 100.0, time.time(), pol, leverage=1)
    d = _tracker_to_dict(tr)
    t2 = _tracker_from_dict(d)
    assert t2.policy.time_scratch_enabled is True
    assert t2.policy.time_scratch_minutes == pytest.approx(60.0)
    assert t2.policy.time_scratch_min_peak_pct == pytest.approx(0.3)
    assert t2.policy.time_scratch_giveback_pct == pytest.approx(0.3)


# ═══════════════════════════════ Part A ════════════════════════════════════

def test_e3_resolve_clocks_falls_back_to_global_without_block():
    from hermes_trader.agents.executor import resolve_regime_clocks
    dsl = {"hard_timeout_minutes": 1800.0, "stale_flat_timeout_minutes": 480.0,
           "regime_aware": {"enabled": True}}
    up = resolve_regime_clocks(dsl, "up")
    neu = resolve_regime_clocks(dsl, "neutral")
    assert up["hard_timeout_minutes"] == pytest.approx(1800.0)
    assert up["stale_flat_timeout_minutes"] == pytest.approx(480.0)
    assert neu["hard_timeout_minutes"] == pytest.approx(1800.0)


def test_e3_resolve_clocks_inert_when_disabled():
    from hermes_trader.agents.executor import resolve_regime_clocks
    dsl = {
        "hard_timeout_minutes": 1800.0, "stale_flat_timeout_minutes": 480.0,
        "regime_aware": {"enabled": True, "clocks": {
            "enabled": False,
            "trend": {"hard_timeout_minutes": 2880.0,
                      "stale_flat_timeout_minutes": 720.0},
            "non_trend": {"hard_timeout_minutes": 960.0,
                          "stale_flat_timeout_minutes": 240.0}}},
    }
    up = resolve_regime_clocks(dsl, "up")
    assert up["hard_timeout_minutes"] == pytest.approx(1800.0)
    assert up["stale_flat_timeout_minutes"] == pytest.approx(480.0)


def test_e3_resolve_clocks_splits_trend_vs_scalp_when_enabled():
    from hermes_trader.agents.executor import resolve_regime_clocks
    dsl = {
        "hard_timeout_minutes": 1800.0, "stale_flat_timeout_minutes": 480.0,
        "regime_aware": {"enabled": True, "clocks": {
            "enabled": True,
            "trend": {"hard_timeout_minutes": 2880.0,
                      "stale_flat_timeout_minutes": 720.0},
            "non_trend": {"hard_timeout_minutes": 960.0,
                          "stale_flat_timeout_minutes": 240.0}}},
    }
    up = resolve_regime_clocks(dsl, "up")
    down = resolve_regime_clocks(dsl, "down")
    neu = resolve_regime_clocks(dsl, "neutral")
    chop = resolve_regime_clocks(dsl, "chop")
    # trend rides get the LONG clock, scalp regimes the SHORT one.
    for c in (up, down):
        assert c["hard_timeout_minutes"] == pytest.approx(2880.0)
        assert c["stale_flat_timeout_minutes"] == pytest.approx(720.0)
    for c in (neu, chop):
        assert c["hard_timeout_minutes"] == pytest.approx(960.0)
        assert c["stale_flat_timeout_minutes"] == pytest.approx(240.0)


def test_e3_canonical_registers_time_scratch_and_clocks_inert():
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS
    dsl = CANONICAL_DEFAULTS["dsl_exit"]
    ts = dsl.get("time_scratch")
    assert isinstance(ts, dict) and ts.get("enabled") is False
    clocks = dsl["regime_aware"].get("clocks")
    assert isinstance(clocks, dict) and clocks.get("enabled") is False


def test_e3_schema_accepts_new_blocks():
    from hermes_trader.agents.config_schema import validate_config_updates
    errs = validate_config_updates({
        "dsl_exit": {
            "time_scratch": {"enabled": True, "minutes": 90.0,
                             "min_peak_pct": 0.4, "giveback_pct": 0.25},
            "regime_aware": {"clocks": {
                "enabled": True,
                "trend": {"hard_timeout_minutes": 2880.0,
                          "stale_flat_timeout_minutes": 720.0},
                "non_trend": {"hard_timeout_minutes": 960.0,
                              "stale_flat_timeout_minutes": 240.0}}},
        },
    })
    flagged = [e for e in errs if "time_scratch" in e or "clocks" in e]
    assert flagged == [], f"new E3 blocks rejected: {flagged}"


def test_e3_build_policy_propagates_time_scratch(monkeypatch):
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": {"time_scratch": {
            "enabled": True, "minutes": 75.0,
            "min_peak_pct": 0.4, "giveback_pct": 0.2}}})
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE", None)
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE_TS", 0.0)
    pol = dsl_exit._build_policy_from_config()
    assert pol.time_scratch_enabled is True
    assert pol.time_scratch_minutes == pytest.approx(75.0)
    assert pol.time_scratch_min_peak_pct == pytest.approx(0.4)
    assert pol.time_scratch_giveback_pct == pytest.approx(0.2)
