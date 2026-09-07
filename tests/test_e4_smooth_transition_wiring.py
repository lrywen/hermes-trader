"""E4 (Audit 2026-09-06, P2 choppy-market) — wire the ``smooth_transition``
dead knob.

``ExitPolicy.smooth_transition_enabled`` / ``smooth_band_pct`` and the ramp
logic (``_smooth_phase2_floor``) already existed and were covered by
``test_p2_smooth_transition.py`` — but NO construction path ever fed the knob
from config, so it was permanently inert. The ramp ships DEFAULT OFF (tick A/B
replay in scripts/p2_smooth_replay.py showed it net-negative); these tests pin
the three construction paths so an operator who DOES opt in via
``dsl_exit.smooth_transition`` actually gets the behavior, and that the default
config leaves the policy byte-inert:

1. canonical ``dsl_exit.smooth_transition`` block is registered (enabled False,
   band_pct 1.0) and is a known schema key (dashboard writes not rejected);
2. ``_build_policy_from_config`` propagates the block → ExitPolicy fields;
3. the rehydrate path (``_tracker_to_dict`` → ``_tracker_from_dict``) preserves
   the fields across a restart (used to silently fall back to False/1.0);
4. with the block absent / disabled the built policy is the inert default.

The executor live-entry construction point (executor.py ~L2055) is the same
knob read; it is covered by the executor registration test suite and kept in
lockstep with _build_policy_from_config.
"""
from __future__ import annotations

import time

import pytest

from hermes_trader.agents import dsl_exit


# ── 1. canonical registration + schema ─────────────────────────────────────

def test_e4_canonical_dsl_exit_has_smooth_transition_block():
    """The canonical dsl_exit block registers smooth_transition (default OFF)."""
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS
    dsl = CANONICAL_DEFAULTS["dsl_exit"]
    blk = dsl.get("smooth_transition")
    assert isinstance(blk, dict)
    assert blk.get("enabled") is False
    # band width is a positive number; 1.0 matches ExitPolicy.smooth_band_pct.
    assert float(blk.get("band_pct", 0.0)) > 0.0


def test_e4_smooth_transition_block_is_known_schema_key():
    """dsl_exit.smooth_transition.{enabled,band_pct} must pass strict config
    validation (dashboard/CLI write path), else an operator could never set it.
    Unknown keys are rejected with '<path>: unknown key'."""
    from hermes_trader.agents.config_schema import validate_config_updates
    errs = validate_config_updates({
        "dsl_exit": {"smooth_transition": {"enabled": True, "band_pct": 1.5}},
    })
    key_errs = [e for e in errs if "smooth_transition" in e]
    assert key_errs == [], f"smooth_transition rejected by schema: {key_errs}"


# ── 2. _build_policy_from_config propagation ───────────────────────────────

@pytest.fixture(autouse=True)
def _reset_policy_cache(monkeypatch):
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE", None)
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE_TS", 0.0)
    yield


def _pin_config(monkeypatch, dsl_block):
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": dsl_block})
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE", None)
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE_TS", 0.0)


def test_e4_build_policy_off_by_default_inert(monkeypatch):
    """With no smooth_transition key the built policy is the inert default."""
    _pin_config(monkeypatch, {"protect_pct": 1.25})
    pol = dsl_exit._build_policy_from_config()
    assert pol.smooth_transition_enabled is False
    assert pol.smooth_band_pct == pytest.approx(1.0)


def test_e4_build_policy_propagates_enabled_block(monkeypatch):
    """An enabled config block flows through to the ExitPolicy fields."""
    _pin_config(monkeypatch, {
        "smooth_transition": {"enabled": True, "band_pct": 2.5},
    })
    pol = dsl_exit._build_policy_from_config()
    assert pol.smooth_transition_enabled is True
    assert pol.smooth_band_pct == pytest.approx(2.5)


def test_e4_build_policy_disabled_block_stays_off(monkeypatch):
    """An explicit enabled=False keeps the ramp inert even with a band set."""
    _pin_config(monkeypatch, {
        "smooth_transition": {"enabled": False, "band_pct": 3.0},
    })
    pol = dsl_exit._build_policy_from_config()
    assert pol.smooth_transition_enabled is False
    assert pol.smooth_band_pct == pytest.approx(3.0)


# ── 3. rehydrate preserves the knob ───────────────────────────────────────

def test_e4_rehydrate_preserves_smooth_fields(monkeypatch):
    """_tracker_to_dict → _tracker_from_dict must round-trip the smooth knob
    (previously rehydrate rebuilt ExitPolicy without the fields, silently
    dropping an enabled ramp after a restart)."""
    from hermes_trader.agents.dsl_exit import ExitPolicy, DSLTracker
    monkeypatch.setattr(dsl_exit, "_request_save", lambda **_k: None)
    pol = ExitPolicy(smooth_transition_enabled=True, smooth_band_pct=2.0)
    t = DSLTracker("E4R", "long", 100.0, time.time(), policy=pol, leverage=10)
    d = dsl_exit._tracker_to_dict(t)
    t2 = dsl_exit._tracker_from_dict(d)
    assert t2.policy.smooth_transition_enabled is True
    assert t2.policy.smooth_band_pct == pytest.approx(2.0)


def test_e4_rehydrate_defaults_inert_when_absent(monkeypatch):
    """An old state file without the fields rehydrates to the inert default."""
    d = {
        "coin": "E4D", "side": "long", "entry_px": 100.0,
        "entry_time": time.time(),
        "policy": {"protect_pct": 1.25, "retrace_threshold": 0.2},
    }
    t = dsl_exit._tracker_from_dict(d)
    assert t.policy.smooth_transition_enabled is False
    assert t.policy.smooth_band_pct == pytest.approx(1.0)
