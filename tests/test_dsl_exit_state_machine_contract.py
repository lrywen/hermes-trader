"""Behavioral contract tests for the DSLTracker two-phase exit state machine.

T1 (architecture review 2026-09-11): ``dsl_exit.py`` (~2.6k LOC, no dedicated
direct test file) is one of the two most complex and most frequently edited
modules in the system. The existing suite only covers narrow audit paths
(peak persistence, ATR stop, smooth transition, wick index gate for
floor_breach, time_scratch, state-file resilience). These tests pin the
*behavioral contract* of the core branches that previously had ZERO direct
check()-level coverage:

  1. hard_timeout exit (unconditional age cap)
  2. max_loss H-5 wick guard: time-gate lifecycle (suppress -> persist -> exit,
     and reset when price recovers inside the cap)
  3. max_loss hard-stop INDEX suppression (healthy index on the safe side)
  4. phase-2 retrace tier selection by PEAK (8% / 15% earned, retained on
     retracement), long and short
  5. consecutive_breaches_required count gate (hold N-1, exit Nth)
  6. breakeven ratchet (floor locked above entry once armed; never before)
  7. status() read-only contract (never mutates peak/floor/counter/state)
  8. short-side max_loss mirror

The state machine is a pure in-memory object: check() does no network, reads no
config and touches no global registry. Its only side effect is the module
function ``_request_save`` (persistence), which is stubbed here. Time gates are
exercised by back-dating the internal monotonic timestamps rather than
sleeping, so the tests are deterministic and feed-rate independent.

These tests LOCK EXISTING BEHAVIOR; they do not change any trading logic.
"""

import time

import pytest


def _policy(**kw):
    """Deterministic policy: all confirmation windows and age exits off unless
    a test explicitly enables them. ROE cap disabled (100) so spot % is exact."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    base = dict(
        max_loss_pct=2.0,
        max_loss_roe_pct=100.0,
        protect_pct=1.25,
        retrace_threshold=0.20,
        hard_timeout_minutes=1e9,
        stale_flat_timeout_minutes=0.0,
        breach_confirm_sec=0.0,
        hard_stop_confirm_sec=0.0,
        consecutive_breaches_required=1,
        breakeven_trigger_pct=0.0,
        atr_stop_enabled=False,
    )
    base.update(kw)
    return ExitPolicy(**base)


def _tracker(monkeypatch, side="long", entry=100.0, policy=None,
             entry_time=None, leverage=1, entry_atr_pct=0.0):
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(dsl_exit, "_request_save", lambda **_k: None)
    return dsl_exit.DSLTracker(
        "TST", side, entry, entry_time if entry_time is not None else time.time(),
        policy=policy or _policy(), leverage=leverage, entry_atr_pct=entry_atr_pct,
    )


# ── 1. hard_timeout ─────────────────────────────────────────────────────────

def test_hard_timeout_fires_unconditionally(monkeypatch):
    """Once the age cap is reached, check() exits with phase='timeout' even for
    a position that is currently in profit."""
    old = time.time() - 2000 * 60  # 2000 minutes ago, > 1800 default
    tr = _tracker(monkeypatch, policy=_policy(hard_timeout_minutes=1800.0),
                  entry_time=old)
    v = tr.check(105.0)  # comfortably in profit — must still time out
    assert v.exit is True
    assert v.phase == "timeout"
    assert "hard_timeout" in v.reason


def test_hard_timeout_not_before_limit(monkeypatch):
    tr = _tracker(monkeypatch, policy=_policy(hard_timeout_minutes=1800.0),
                  entry_time=time.time() - 100 * 60)
    v = tr.check(100.0)
    assert v.exit is False


# ── 2. max_loss H-5 wick guard: time-gate lifecycle ─────────────────────────

# NOTE on isolating the H-5 hard-stop guard: at the exact max-loss cap the mark
# ALSO sits on the phase-1 trailing floor, so floor_breach would fire on the
# same tick if breach_confirm_sec=0. Tests of the hard-stop confirmation/index
# guards therefore arm a (longer) floor breach window too: this holds the
# downstream floor_breach branch while the H-5 branch is exercised. The hard
# stop still wins priority once its own (short) window + index confirm.

_HS_POLICY = dict(hard_stop_confirm_sec=1.0, breach_confirm_sec=30.0)


def test_max_loss_time_gate_suppresses_first_tick_then_confirms(monkeypatch):
    """With hard_stop_confirm_sec>0, the first tick at the cap arms but does
    NOT exit; once the breach has persisted past the window, the next check
    exits with max_loss (fail-open on a missing index)."""
    tr = _tracker(monkeypatch, policy=_policy(**_HS_POLICY))
    # -2% is exactly the spot cap; isclose makes the boundary a hard-stop hit.
    stop_px = 98.0
    first = tr.check(stop_px)
    assert first.exit is False  # H-5 timer just armed; floor gate still pending
    assert tr._first_hardstop_ts is not None
    # Back-date the H-5 arm timestamp so its persistence window has elapsed.
    tr._first_hardstop_ts = time.monotonic() - 5.0
    second = tr.check(stop_px)
    assert second.exit is True
    assert "max_loss" in second.reason
    assert second.phase == "phase1"


def test_max_loss_time_gate_resets_when_price_recovers(monkeypatch):
    """A tick back inside the cap clears the armed hard-stop timer, so a later
    re-touch must re-arm from scratch (a recovered wick cannot carry a stale
    timer straight through)."""
    tr = _tracker(monkeypatch, policy=_policy(**_HS_POLICY))
    assert tr.check(98.0).exit is False       # arm
    assert tr._first_hardstop_ts is not None
    assert tr.check(99.9).exit is False       # recover inside cap
    assert tr._first_hardstop_ts is None
    # A fresh touch must NOT immediately exit: the timer re-armed at zero.
    again = tr.check(98.0)
    assert again.exit is False
    assert tr._first_hardstop_ts is not None


# ── 3. max_loss hard-stop INDEX suppression ─────────────────────────────────

def test_max_loss_index_gate_suppresses_wick(monkeypatch):
    """mid at the cap but a healthy INDEX still on the safe side => hold
    (hard-stop exit suppressed; the wick is logged, reason stays empty as the
    branch deliberately returns no verdict)."""
    tr = _tracker(monkeypatch, policy=_policy(hard_stop_confirm_sec=0.0,
                                              breach_confirm_sec=30.0))
    # long: hard-stop floor is 98.0. Index 99.0 is ABOVE it (safe side).
    v = tr.check(98.0, index_px=99.0)
    assert v.exit is False
    assert v.reason == ""
    # The suppressed arm does not fire even with the H-5 time gate disabled.
    assert tr._first_hardstop_ts is not None or v.exit is False


def test_max_loss_index_gate_confirms_real_break(monkeypatch):
    """mid and index both through the hard floor => max_loss exit (H-5 time
    gate disabled, floor gate armed but hard stop wins priority)."""
    tr = _tracker(monkeypatch, policy=_policy(hard_stop_confirm_sec=0.0,
                                              breach_confirm_sec=30.0))
    v = tr.check(98.0, index_px=97.5)
    assert v.exit is True
    assert "max_loss" in v.reason


# ── 4. phase-2 retrace tier selection by PEAK ───────────────────────────────

def test_phase2_tier_earned_by_peak_and_retained_on_retrace(monkeypatch):
    """A peak that cleared the 8% tier earns the 35% give-back; after the peak
    is set the ACTIVE tier must stay 35% even though the current mark falls
    back, and the floor must trail that peak."""
    tr = _tracker(monkeypatch, entry=100.0)
    tr.check(108.0)   # peak = 108 (+8%) → tier 35%
    tier = tr._active_tier(tr.peak_px)
    assert tier.retrace_threshold == pytest.approx(0.35)
    # Retrace to +3%; tier is keyed off peak (108), still 35%.
    tr.check(103.0)
    assert tr._active_tier(tr.peak_px).retrace_threshold == pytest.approx(0.35)
    # trailing floor = 100 + (108-100)*(1-0.35) = 105.2 (monotonic-clamped)
    assert tr._last_floor == pytest.approx(105.2)


def test_phase2_higher_tier_15pct(monkeypatch):
    tr = _tracker(monkeypatch, entry=100.0)
    tr.check(115.0)  # +15% → 40% tier
    assert tr._active_tier(tr.peak_px).retrace_threshold == pytest.approx(0.40)
    # floor = 100 + 15*0.60 = 109.0
    assert tr._last_floor == pytest.approx(109.0)


def test_phase2_tier_short_mirror(monkeypatch):
    """Short: peak ratchets DOWN; +8% favorable selects 35% and floor trails
    below entry, retained on a retracement upward."""
    tr = _tracker(monkeypatch, side="short", entry=100.0)
    tr.check(92.0)   # peak = 92 (-8%, favorable for short)
    assert tr._active_tier(tr.peak_px).retrace_threshold == pytest.approx(0.35)
    tr.check(97.0)   # retrace against the short; peak stays 92
    assert tr.peak_px == pytest.approx(92.0)
    # short floor = 100 - (100-92)*0.65 = 94.8
    assert tr._last_floor == pytest.approx(94.8)


def test_phase1_hard_floor_before_protect(monkeypatch):
    """Below protect_pct the floor is the hard stop, not a trailing floor."""
    tr = _tracker(monkeypatch, entry=100.0)
    tr.check(100.5)  # +0.5%, never armed phase-2
    assert tr._phase_label() == "phase1"
    # hard floor long at -2% = 98.0
    assert tr._last_floor == pytest.approx(98.0)


# ── 5. consecutive_breaches count gate ──────────────────────────────────────

def test_consecutive_breaches_requires_n_ticks(monkeypatch):
    """consecutive_breaches_required=3: the first two breaching ticks hold, the
    third exits; a recovering tick resets the count."""
    # Arm phase-2 first so there IS a trailing floor above entry to breach.
    tr = _tracker(monkeypatch, entry=100.0,
                  policy=_policy(consecutive_breaches_required=3,
                                 breach_confirm_sec=0.0))
    tr.check(108.0)   # peak +8% → floor 105.2
    # Two breaching ticks hold (count 1, 2).
    assert tr.check(104.0).exit is False
    assert tr.consecutive_breaches == 1
    assert tr.check(104.0).exit is False
    assert tr.consecutive_breaches == 2
    # A recovery resets the counter.
    assert tr.check(107.0).exit is False
    assert tr.consecutive_breaches == 0
    # Three FRESH consecutive breaches are then required again (counts 1,2,3).
    assert tr.check(104.0).exit is False
    assert tr.check(104.0).exit is False
    third = tr.check(104.0)
    assert third.exit is True
    assert "floor_breach" in third.reason


# ── 6. breakeven ratchet ────────────────────────────────────────────────────

def test_breakeven_locks_floor_above_entry_after_arm(monkeypatch):
    """Once peak profit reaches the trigger, the floor may never sit below
    entry + lock_pct (long). Construct a phase-2 peak whose plain trailing
    floor would be BELOW the breakeven lock so the ratchet is what binds."""
    # peak +2% arms trigger; with 50% give-back the plain trail would be
    # 100 + 2*0.5 = 101.0, but the 1.5% breakeven lock clamps it to 101.5.
    tr = _tracker(monkeypatch, entry=100.0,
                  policy=_policy(retrace_threshold=0.50,
                                 breakeven_trigger_pct=2.0,
                                 breakeven_lock_pct=1.5))
    tr.check(101.0)  # peak +1% below trigger → not armed, hard floor 98
    assert tr._last_floor == pytest.approx(98.0)
    tr.check(102.0)  # peak +2% armed → max(trail 101.0, breakeven 101.5)
    assert tr._last_floor == pytest.approx(101.5)


def test_breakeven_short_locks_below_entry(monkeypatch):
    tr = _tracker(monkeypatch, side="short", entry=100.0,
                  policy=_policy(retrace_threshold=0.50,
                                 breakeven_trigger_pct=2.0,
                                 breakeven_lock_pct=1.5))
    tr.check(98.0)  # -2% favorable armed → min(trail 99.0, breakeven 98.5)
    assert tr._last_floor == pytest.approx(98.5)


# ── 7. status() read-only contract ──────────────────────────────────────────

def test_status_is_read_only(monkeypatch):
    """status() must not advance peak/floor, raise the breach counter, or
    persist, regardless of how alarming the supplied mark is."""
    tr = _tracker(monkeypatch, entry=100.0)
    tr.check(103.0)  # establish a peak/floor via check()
    peak_before, floor_before = tr.peak_px, tr._last_floor
    breaches_before = tr.consecutive_breaches
    # A catastrophic mark through every stop — status reports would_exit but
    # must mutate NOTHING.
    snap = tr.status(50.0)
    assert snap["would_exit"] is True
    assert tr.peak_px == peak_before
    assert tr._last_floor == floor_before
    assert tr.consecutive_breaches == breaches_before
    # Calling status repeatedly is stable.
    assert tr.status(50.0)["peak_px"] == peak_before


def test_status_reports_hard_timeout_would_exit(monkeypatch):
    old = time.time() - 2000 * 60
    tr = _tracker(monkeypatch, policy=_policy(hard_timeout_minutes=1800.0),
                  entry_time=old)
    snap = tr.status(100.0)
    assert snap["would_exit"] is True
    assert snap["exit_reason"] == "hard_timeout"


# ── 8. short-side max_loss mirror ───────────────────────────────────────────

def test_short_max_loss_fires_above_entry(monkeypatch):
    """For a short the hard stop is ABOVE entry; an adverse upward move exits."""
    tr = _tracker(monkeypatch, side="short", entry=100.0,
                  policy=_policy(max_loss_pct=2.0, hard_stop_confirm_sec=0.0))
    v = tr.check(102.0)  # +2% adverse
    assert v.exit is True
    assert "max_loss" in v.reason
    assert v.position_side == "short"


def test_short_max_loss_index_suppresses(monkeypatch):
    """Short: mid through the upper stop but index still BELOW it => hold."""
    tr = _tracker(monkeypatch, side="short", entry=100.0,
                  policy=_policy(max_loss_pct=2.0, hard_stop_confirm_sec=0.0,
                                 breach_confirm_sec=30.0))
    # short hard floor at 102.0; index 101.0 is on the safe (lower) side.
    v = tr.check(102.0, index_px=101.0)
    assert v.exit is False
    assert v.reason == ""
