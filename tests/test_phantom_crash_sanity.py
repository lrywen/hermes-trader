"""C1: heartbeat phantom-crash sanity check (ARCHITECTURE.md "equity-spike bug").

A NON-zero degraded aggregate (a dex under-reports instead of dropping out)
reads as a crash-sized equity drop with no real close behind it. The loop's
``phantom_crash_unconfirmed`` helper distinguishes that phantom from a genuine
liquidation/close: a crash-sized single-tick drop is only treated as transient
when NO real-close event (close/dsl_exit/ai_close/external_close_recorded/
hard_killswitch) lands in the recent events.jsonl window.

The helper lives in scripts/trading_loop.py, whose module-level ``while True``
cannot be imported — so it is lifted out via AST (same loader pattern as
test_audit_p1.py's ``_load_loop_fn``).
"""
import ast
import logging
import time
from pathlib import Path


def _load_phantom_guard():
    """Extract phantom_crash_unconfirmed + _PHANTOM_CRASH_EXIT_EVENTS from
    scripts/trading_loop.py and exec them in a controlled namespace."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "scripts" / "trading_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"phantom_crash_unconfirmed", "_PHANTOM_CRASH_EXIT_EVENTS"}
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in wanted)
             or (isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) in wanted for t in n.targets))]
    ns = {
        "logger": logging.getLogger("test.phantom_crash"),
        "time": time,
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, "<trading_loop extracted>", "exec"), ns)
    return ns["phantom_crash_unconfirmed"], ns["_PHANTOM_CRASH_EXIT_EVENTS"]


guard, EXIT_EVENTS = _load_phantom_guard()

NOW = 1_000_000.0


def _q(events):
    """Build a query_fn stub returning the given events (query_events shape:
    forked records carry ``event`` at top level, payload-nested also accepted)."""
    return lambda start=None: list(events)


# ── Phantom: crash-sized drop, nothing explains it → reject the reading ──

def test_phantom_crash_no_close_event_is_flagged():
    # 100 -> 50 (-50%) within one tick, no recent real-close event.
    assert guard(50.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_q([]), now=NOW) is True


def test_phantom_crash_just_at_threshold_is_flagged():
    # 100 -> 60 is exactly -40% (move_frac <= -crash_pct) → crash-sized.
    assert guard(60.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_q([]), now=NOW) is True


# ── Genuine crash: a real-close event explains the drop → accept ──

def test_real_crash_with_close_event_is_accepted():
    for ev in ("close", "dsl_exit", "ai_close",
               "external_close_recorded", "hard_killswitch"):
        assert guard(50.0, 100.0, NOW - 15, crash_pct=0.40,
                     query_fn=_q([{"event": ev}]), now=NOW) is False, ev


def test_real_crash_payload_nested_event_is_accepted():
    # forked records as event_log.query_events returns them: fields under
    # payload, but the top-level "event" is also set — exercise the nested
    # fallback shape defensively.
    rec = {"event": "unrelated", "payload": {"event": "dsl_exit", "coin": "ETH"}}
    assert guard(50.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_q([rec]), now=NOW) is False


def test_unrelated_event_does_not_explain_crash():
    # A scan/research/heartbeat event is not a real close → still phantom.
    assert guard(50.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_q([{"event": "scan"}, {"event": "heartbeat"}]),
                 now=NOW) is True


# ── Fail-OPEN guards: ambiguity/missing evidence must accept the reading ──

def test_no_query_fn_fails_open():
    assert guard(50.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=None, now=NOW) is False


def test_query_raises_fails_open():
    def _boom(start=None):
        raise RuntimeError("events.jsonl unreadable")
    assert guard(50.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_boom, now=NOW) is False


def test_no_previous_reading_accepts():
    # First tick after restart (prev_eq=0) → no baseline to compare.
    assert guard(50.0, 0.0, 0.0, crash_pct=0.40,
                 query_fn=_q([]), now=NOW) is False


def test_stale_previous_reading_accepts():
    # Last accepted reading is older than the recency window (loop was stalled
    # long ago); a big diff over a long gap is not a "single-tick" crash.
    assert guard(50.0, 100.0, NOW - 200, crash_pct=0.40, recency_s=180.0,
                 query_fn=_q([]), now=NOW) is False


def test_sub_threshold_drop_accepts():
    # -39% is below the 40% crash threshold (memory's implausible/reconfirm
    # layer handles this band) — not this guard's job.
    assert guard(61.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_q([]), now=NOW) is False


def test_nonpositive_crash_pct_disables_check():
    assert guard(1.0, 100.0, NOW - 15, crash_pct=0.0,
                 query_fn=_q([]), now=NOW) is False
    assert guard(1.0, 100.0, NOW - 15, crash_pct=None,
                 query_fn=_q([]), now=NOW) is False


def test_nonpositive_equity_never_phantoms():
    # equity<=0 / prev<=0 is handled by the earlier degraded-read guards.
    assert guard(0.0, 100.0, NOW - 15, crash_pct=0.40,
                 query_fn=_q([]), now=NOW) is False


def test_exit_event_whitelist_matches_event_log_forkables():
    # Every explaining event must actually be persisted to events.jsonl
    # (event_log._FORKABLE_EVENTS), plus "close" which event_log.append writes
    # directly from memory.record_close. Guard against whitelist drift.
    from hermes_trader import event_log
    for ev in EXIT_EVENTS:
        if ev == "close":
            continue  # written via event_log.append("close"), not forked
        assert ev in event_log._FORKABLE_EVENTS, ev
