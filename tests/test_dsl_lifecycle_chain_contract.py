"""End-to-end LIFECYCLE chain contracts for the DSL exit machinery (T1).

The unit-level contracts (state machine, entry gate, bracket price/size,
incident regressions) already pin each primitive in isolation. This file
pins the two full chains called out in the 2026-09-11 architecture review,
where the value is in the SEQUENCE, not in any single branch:

  Chain 1 — open → bracket → DSL tracking:
      register_position → set_bracket (persisted) → repeated ticks across
      phase1 / phase2 arm / peak ratchet / monotonic floor → restart
      rehydration mid-chain → long/short mirror → registry lifecycle.

  Chain 2 — DSL → exit:
      max_loss (time/index wick gates, ROE cap, recovery reset),
      phase2 floor_breach (time + consecutive-count + index gates),
      hard_timeout / stale_flat / time_scratch, exit telemetry, and the
      final deregister that closes the lifecycle.

Determinism: all time-confirmation windows are set to 0 via the policy unless
a test specifically exercises the gate, in which case the monotonic timestamp
is backdated. The registry is isolated to tmp_path so no test touches the
live state file. Pure additive tests — no production code is modified.
"""
from __future__ import annotations

import logging
import time

import pytest

from hermes_trader.agents import dsl_exit

# ── isolation + shared builders ─────────────────────────────────────────

@pytest.fixture
def isolated_dsl(monkeypatch, tmp_path):
    """Point the DSL registry at tmp_path and reset every process global."""
    state_file = tmp_path / "dsl_lifecycle_chain.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    monkeypatch.setattr(dsl_exit, "DSL_STATE_LOCK_FILE", str(state_file) + ".lock")
    monkeypatch.setattr(dsl_exit, "_LAST_SAVE_TS", 0.0)
    monkeypatch.setattr(dsl_exit, "_SAVE_DIRTY", False)
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    yield
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False


def _policy(**overrides):
    """Deterministic policy: both wick-confirmation windows closed by default."""
    params = {"hard_stop_confirm_sec": 0.0, "breach_confirm_sec": 0.0}
    params.update(overrides)
    return dsl_exit.ExitPolicy(**params)


def _restart_rehydrate():
    """Simulate a process restart: drop the in-memory registry and reload."""
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit.load_state()


# ════════════════════════════════════════════════════════════════════════
# Chain 1 — open → bracket → DSL lifecycle (12 cases)
# ════════════════════════════════════════════════════════════════════════

def test_c1_01_register_tracks_active_coin_and_deregister_is_idempotent(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    assert t.coin == "BTC" and t.side == "long"
    assert dsl_exit.active_position_coins() == {"BTC": "long"}

    assert dsl_exit.deregister_position("BTC", "long") is True
    assert dsl_exit.active_position_coins() == {}
    # Idempotent: closing an already-closed tracker is a quiet False, not raise.
    assert dsl_exit.deregister_position("BTC", "long") is False
    assert dsl_exit.deregister_position("BTC", "short") is False


def test_c1_02_register_persists_immediately_and_rehydrates(isolated_dsl):
    dsl_exit.register_position(
        "ETH", "short", entry_px=200.0, policy=_policy(), leverage=3,
        entry_regime="down")

    _restart_rehydrate()
    t = dsl_exit._active_positions["ETH_short"]
    assert t.entry_px == pytest.approx(200.0)
    assert t.leverage == 3
    assert t.entry_regime == "down"
    assert t.peak_px == pytest.approx(200.0)
    assert t._last_floor is None


def test_c1_03_duplicate_register_warns_and_resets_ratchet(isolated_dsl, caplog):
    t = dsl_exit.register_position("SOL", "long", entry_px=100.0, policy=_policy())
    t.check(102.0)  # build a peak + phase2 floor
    assert t.peak_px == pytest.approx(102.0)
    assert t._last_floor is not None

    with caplog.at_level(logging.WARNING, logger="hermes_trader.agents.dsl_exit"):
        t2 = dsl_exit.register_position("SOL", "long", entry_px=110.0, policy=_policy())

    assert any("OVERWRITES existing tracker" in r.message for r in caplog.records)
    # The replacement starts from the NEW entry: stale ratchet state must not
    # silently carry over onto what is economically a new position.
    assert t2.entry_px == pytest.approx(110.0)
    assert t2.peak_px == pytest.approx(110.0)
    assert t2._last_floor is None


def test_c1_04_set_bracket_coerces_persists_and_rehydrates(isolated_dsl):
    dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    ok = dsl_exit.set_bracket(
        "BTC", "long", sl_oid="123", sl_px="99.5", sl_size="2.5",
        tp_oid=456, tp_px=105.0)
    assert ok is True
    t = dsl_exit._active_positions["BTC_long"]
    assert t.sl_oid == 123 and isinstance(t.sl_oid, int)
    assert t.sl_px == pytest.approx(99.5)
    assert t.sl_size == pytest.approx(2.5)
    assert t.tp_oid == 456
    assert t.tp_px == pytest.approx(105.0)

    dsl_exit._save_state()
    _restart_rehydrate()
    r = dsl_exit._active_positions["BTC_long"]
    assert (r.sl_oid, r.tp_oid) == (123, 456)
    assert r.sl_px == pytest.approx(99.5)
    assert r.sl_size == pytest.approx(2.5)
    assert r.tp_px == pytest.approx(105.0)


def test_c1_05_set_bracket_rejects_missing_tracker_and_unknown_fields(isolated_dsl):
    assert dsl_exit.set_bracket("NOPE", "long", sl_oid=1) is False

    dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    assert dsl_exit.set_bracket("BTC", "long", bogus_field=999, sl_px=99.0) is True
    t = dsl_exit._active_positions["BTC_long"]
    assert getattr(t, "bogus_field", "sentinel") == "sentinel"
    assert t.sl_px == pytest.approx(99.0)
    # Un-coercible oid degrades to None rather than storing garbage.
    dsl_exit.set_bracket("BTC", "long", sl_oid="not-an-int")
    assert t.sl_oid is None


def test_c1_06_bracket_fields_survive_the_full_check_cycle(isolated_dsl):
    dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    dsl_exit.set_bracket("BTC", "long", sl_oid=111, sl_px=99.6,
                         sl_size=1.0, tp_oid=222, tp_px=105.0)

    # phase1 hold → phase2 arm → pullback clamp: check() must never clobber
    # the exchange-bracket bookkeeping the reconciler relies on.
    for mark in (99.8, 102.0, 101.7):
        v = dsl_exit._active_positions["BTC_long"].check(mark)
        assert v.exit is False
        t = dsl_exit._active_positions["BTC_long"]
        assert (t.sl_oid, t.tp_oid) == (111, 222)
        assert t.sl_px == pytest.approx(99.6)
        assert t.sl_size == pytest.approx(1.0)
        assert t.tp_px == pytest.approx(105.0)


def test_c1_07_phase1_tick_holds_at_hard_stop_floor(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    v = t.check(99.8)  # -0.2% spot, inside the 0.4% effective cap
    assert v.exit is False
    assert v.phase == "phase1"
    assert v.floor_price == pytest.approx(99.6)
    assert v.peak_price == pytest.approx(100.0)
    assert t._last_floor == pytest.approx(99.6)


def test_c1_08_phase2_arms_on_peak_and_floor_trails(isolated_dsl):
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0,
        policy=_policy(protect_pct=1.25, retrace_threshold=0.20))
    v = t.check(102.0)  # +2% peak clears protect → trail = 100 + 2*0.8
    assert v.exit is False
    assert v.phase == "phase2"
    assert v.peak_price == pytest.approx(102.0)
    assert v.floor_price == pytest.approx(101.6)


def test_c1_09_phase2_floor_ratchets_monotonic_on_pullback_and_reextend(isolated_dsl):
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, policy=_policy())
    assert t.check(102.0).floor_price == pytest.approx(101.6)

    # Pullback: raw trail would loosen to 101.28, the clamp holds 101.6.
    v = t.check(101.7)
    assert v.exit is False
    assert v.floor_price == pytest.approx(101.6)

    # New favorable extreme tightens the floor again to 102.4.
    v = t.check(103.0)
    assert v.floor_price == pytest.approx(102.4)


def test_c1_10_phase_label_keys_off_peak_not_current_mark(isolated_dsl):
    # Wide retrace so the armed floor (100.20) sits below protect (101.25),
    # leaving a price (101.0) that is under protect yet above the floor.
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0,
        policy=_policy(protect_pct=1.25, retrace_threshold=0.90))
    assert t.check(102.0).phase == "phase2"
    v = t.check(101.0)  # current profit 1.0% < protect, but peak already armed
    assert v.exit is False
    assert v.phase == "phase2"
    assert v.floor_price == pytest.approx(100.2)


def test_c1_11_short_mirror_phase1_phase2_floor_ratchet(isolated_dsl):
    t = dsl_exit.register_position("BTC", "short", entry_px=100.0, policy=_policy())
    v = t.check(100.2)
    assert v.exit is False
    assert v.phase == "phase1"
    assert v.floor_price == pytest.approx(100.4)  # hard stop ABOVE entry

    v = t.check(98.0)  # -2% favorable → floor = 100 - 2*0.8 = 98.4
    assert v.phase == "phase2"
    assert v.floor_price == pytest.approx(98.4)

    # Bounce: raw trail loosens to 98.72; short clamp (min) holds 98.4.
    assert t.check(98.3).floor_price == pytest.approx(98.4)
    # New favorable extreme tightens DOWN to 97.6.
    assert t.check(97.0).floor_price == pytest.approx(97.6)


def test_c1_12_peak_floor_and_bracket_survive_restart_mid_chain(isolated_dsl):
    dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    dsl_exit.set_bracket("BTC", "long", sl_oid=777, sl_px=99.6, tp_oid=888)
    t = dsl_exit._active_positions["BTC_long"]
    t.check(102.0)  # arm phase2, floor 101.6
    dsl_exit._save_state()

    _restart_rehydrate()
    t = dsl_exit._active_positions["BTC_long"]
    assert t.peak_px == pytest.approx(102.0)
    assert t._last_floor == pytest.approx(101.6)
    assert (t.sl_oid, t.tp_oid, t.sl_px) == (777, 888, pytest.approx(99.6))
    assert t.policy.hard_stop_confirm_sec == 0.0

    # The restored floor remains the ratchet anchor after restart.
    v = t.check(101.7)
    assert v.exit is False
    assert v.floor_price == pytest.approx(101.6)


# ════════════════════════════════════════════════════════════════════════
# Chain 2 — DSL → max_loss / floor_breach / timeout exits (18 cases)
# ════════════════════════════════════════════════════════════════════════

def test_c2_01_max_loss_fires_immediately_with_time_gate_disabled(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    v = t.check(99.6)  # exactly -0.4% spot → isclose must award it to max_loss
    assert v.exit is True
    assert v.reason.startswith("max_loss (")
    assert v.phase == "phase1"
    assert v.floor_price == pytest.approx(99.6)


def test_c2_02_max_loss_roe_cap_binds_under_leverage(isolated_dsl):
    pol = _policy(max_loss_pct=0.4, max_loss_roe_pct=2.4)  # /12x = 0.2% spot
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, policy=pol, leverage=12)
    assert t._effective_max_loss() == pytest.approx(0.2)
    v = t.check(99.8)
    assert v.exit is True
    assert v.reason.startswith("max_loss (")
    assert "roe_cap=2.4/12x" in v.reason
    assert v.floor_price == pytest.approx(99.8)


def test_c2_03_hard_stop_time_gate_holds_first_tick_then_confirms(isolated_dsl):
    # The phase1 floor sits AT the hard-stop price, so the floor gate must be
    # given a window too - otherwise it, not max_loss, is what fires.
    pol = _policy(hard_stop_confirm_sec=0.05, breach_confirm_sec=0.05)
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=pol)

    v = t.check(99.6)  # first tick at the cap: timer just armed → hold
    assert v.exit is False
    assert t._first_hardstop_ts is not None

    t._first_hardstop_ts = time.monotonic() - 0.1  # breach aged past window
    v = t.check(99.6)
    assert v.exit is True
    assert v.reason.startswith("max_loss (")
    assert "held" in v.reason


def test_c2_04_hard_stop_index_wick_suppresses_then_index_confirms(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())

    # Mid at the cap but index (oracle) safely above → single-book wick, hold.
    v = t.check(99.6, index_px=99.9)
    assert v.exit is False
    assert v.reason == ""

    # Index now confirms through the stop → exit fires, labelled idx-confirmed.
    v = t.check(99.6, index_px=99.5)
    assert v.exit is True
    assert v.reason.startswith("max_loss (")
    assert "idx-confirmed" in v.reason


def test_c2_05_recovery_inside_cap_resets_hardstop_timer(isolated_dsl):
    pol = _policy(hard_stop_confirm_sec=0.05, breach_confirm_sec=0.05)
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=pol)

    assert t.check(99.6).exit is False
    assert t._first_hardstop_ts is not None

    assert t.check(99.9).exit is False  # back inside the cap
    assert t._first_hardstop_ts is None

    v = t.check(99.6)  # re-touch arms a FRESH timer, so it must hold again
    assert v.exit is False
    assert t._first_hardstop_ts is not None


def test_c2_06_missing_index_fails_open_to_mid(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    v = t.check(99.6, index_px=None)
    assert v.exit is True
    assert v.reason.startswith("max_loss (")


def test_c2_07_invalid_nan_index_fails_open_to_mid(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    v = t.check(99.6, index_px=float("nan"))
    assert v.exit is True
    assert v.reason.startswith("max_loss (")


def test_c2_08_phase2_floor_breach_exits_through_all_gates(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    assert t.check(102.0).exit is False  # arm, floor 101.6

    v = t.check(101.6)  # mark exactly on the trail floor → breach
    assert v.exit is True
    assert v.reason.startswith("floor_breach (1x consec")
    assert "floor=101.6" in v.reason
    assert v.phase == "phase2"
    assert v.peak_price == pytest.approx(102.0)


def test_c2_09_floor_breach_index_wick_holds_then_recovery_clears_counter(isolated_dsl):
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    t.check(102.0)  # floor 101.6

    v = t.check(101.6, index_px=102.0)  # mid breached, oracle did not
    assert v.exit is False
    assert t.consecutive_breaches == 1
    assert t._first_breach_ts is not None

    v = t.check(102.0)  # recovered above the floor
    assert v.exit is False
    assert t.consecutive_breaches == 0
    assert t._first_breach_ts is None


def test_c2_10_consecutive_breaches_gate_requires_two_ticks(isolated_dsl):
    pol = _policy(consecutive_breaches_required=2)
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=pol)
    t.check(102.0)

    assert t.check(101.6).exit is False
    assert t.consecutive_breaches == 1
    v = t.check(101.6)
    assert v.exit is True
    assert v.reason.startswith("floor_breach (2x consec")


def test_c2_11_breach_time_gate_persists_across_ticks(isolated_dsl):
    pol = _policy(breach_confirm_sec=0.05)
    t = dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=pol)
    t.check(102.0)

    v = t.check(101.6)  # breach timer just armed → hold
    assert v.exit is False
    t._first_breach_ts = time.monotonic() - 0.1
    v = t.check(101.6)  # persisted breach, now older than the window
    assert v.exit is True
    assert v.reason.startswith("floor_breach (")
    assert "held" in v.reason


def test_c2_12_hard_timeout_exits_with_timeout_phase(isolated_dsl):
    pol = _policy(stale_flat_timeout_minutes=0.0, hard_timeout_minutes=5.0)
    entry_t = time.time() - 600  # 10 minutes old
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, entry_time=entry_t, policy=pol)
    v = t.check(100.0)
    assert v.exit is True
    assert v.reason.startswith("hard_timeout (")
    assert v.phase == "timeout"
    assert v.hold_min == pytest.approx(10.0, abs=0.5)


def test_c2_13_stale_flat_timeout_exits_unarmed_position(isolated_dsl):
    pol = _policy(stale_flat_timeout_minutes=5.0, hard_timeout_minutes=1e6)
    entry_t = time.time() - 600
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, entry_time=entry_t, policy=pol)
    v = t.check(100.1)  # +0.1% peak, never armed phase2
    assert v.exit is True
    assert v.reason.startswith("stale_flat_timeout (")
    assert v.phase == "timeout"


def test_c2_14_stale_flat_exempt_once_phase2_ever_armed(isolated_dsl):
    pol = _policy(stale_flat_timeout_minutes=5.0, hard_timeout_minutes=1e6)
    entry_t = time.time() - 600
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, entry_time=entry_t, policy=pol)
    assert t.check(102.0).exit is False  # armed phase2 in the past...
    v = t.check(101.7)                   # ...now aged and retracing, no breach
    assert v.exit is False
    assert v.phase == "phase2"


def test_c2_15_time_scratch_exits_fading_green_pulse(isolated_dsl):
    pol = _policy(
        time_scratch_enabled=True, time_scratch_minutes=5.0,
        time_scratch_min_peak_pct=0.3, time_scratch_giveback_pct=0.3,
        stale_flat_timeout_minutes=0.0, hard_timeout_minutes=1e6)
    entry_t = time.time() - 600
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, entry_time=entry_t, policy=pol)
    assert t.check(100.5).exit is False  # +0.5% pulse, no giveback yet
    v = t.check(100.1)                   # still green, gave back 0.4%
    assert v.exit is True
    assert v.reason.startswith("time_scratch (")
    assert v.phase == "timeout"


def test_c2_16_exit_verdict_carries_full_telemetry(isolated_dsl):
    t = dsl_exit.register_position(
        "BTC", "long", entry_px=100.0, policy=_policy(),
        leverage=5, entry_regime="up")
    assert t.check(101.0).exit is False  # print a +1% MFE, never arm phase2
    v = t.check(99.6)                    # max_loss afterwards
    assert v.exit is True
    assert v.coin == "BTC"
    assert v.position_side == "long"
    assert v.leverage == 5
    assert v.entry_regime == "up"
    assert v.mfe_pct == pytest.approx(1.0)
    assert v.hold_min >= 0.0
    assert v.unrealized_pct == pytest.approx(-0.4)


def test_c2_17_short_max_loss_mirror(isolated_dsl):
    t = dsl_exit.register_position("BTC", "short", entry_px=100.0, policy=_policy())
    v = t.check(100.4)  # -0.4% adverse for a short
    assert v.exit is True
    assert v.reason.startswith("max_loss (")
    assert v.position_side == "short"
    assert v.floor_price == pytest.approx(100.4)


def test_c2_18_full_chain_register_arm_breach_exit_deregister_persists_empty(isolated_dsl):
    dsl_exit.register_position("BTC", "long", entry_px=100.0, policy=_policy())
    assert set(dsl_exit.active_position_coins()) == {"BTC"}
    t = dsl_exit._active_positions["BTC_long"]
    assert t.check(102.0).exit is False
    assert t.check(101.6).exit is True

    assert dsl_exit.deregister_position("BTC", "long") is True
    assert dsl_exit.active_position_coins() == {}

    # The closed lifecycle is durable: a restart sees an empty registry,
    # which is what keeps the re-entry guard from pyramiding a ghost.
    _restart_rehydrate()
    assert dsl_exit._active_positions == {}
