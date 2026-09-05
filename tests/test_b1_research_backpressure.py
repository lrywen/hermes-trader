"""B-1: per-scan research-jobs backpressure cap — trading_loop source wiring.

The truncation itself lives inline in scripts/trading_loop.py's main scan
``while True`` loop (not importable), so — mirroring test_p1_6 / test_market_circuit
— we lock the behaviour with source-level structural assertions. This guards
the invariants reviewed for the 2026-09-04 B-1 change:

  1. the cap is read from the resolved runtime leaf and the drop branch is
     gated ``_jobs_cap > 0 and len(_research_jobs) > _jobs_cap`` (cap<=0 →
     no trim, identical to pre-change);
  2. kept jobs are the top-N by score desc (``sort(..., reverse=True)`` then
     ``[:_jobs_cap]``);
  3. EACH dropped coin emits a ``ta_skip`` audit log_event (signal
     ``JOBS_BACKPRESSURE``) so backpressure drops land in the event hash-chain
     / feed, not only the stdout cycle summary — the 2026-09-04 review's
     observability gap;
  4. every state stamp written at enqueue is rolled back for a dropped coin:
     ``_last_research_by_coin.pop`` / ``_last_research_score_by_coin.pop``
     AND the cycle-persistent content-dedup fingerprint via
     ``_researched_signal_fps.discard`` (discard, not remove — idempotent and
     guarded by the same ``is not None`` check as the enqueue ``add``);
  5. the drop reason recorded in _cycle_outcomes is ``jobs_backpressure_cap``.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRADING_LOOP_SRC = (REPO_ROOT / "scripts" / "trading_loop.py").read_text(encoding="utf-8")


def _drop_branch_src() -> str:
    """The truncation block, from the cap-read line through the warning log."""
    start = TRADING_LOOP_SRC.index("_jobs_cap = ")
    end = TRADING_LOOP_SRC.index("low_score_thr=")
    return TRADING_LOOP_SRC[start:end]


def test_b1_cap_leaf_is_consumed_and_gated():
    assert '_rt["research_max_jobs_per_scan"]' in TRADING_LOOP_SRC
    branch = _drop_branch_src()
    # Guard: cap<=0 or jobs<=cap → no trim (pre-change behaviour).
    assert "_jobs_cap > 0 and len(_research_jobs) > _jobs_cap" in branch


def test_b1_keeps_top_n_by_score_desc():
    branch = _drop_branch_src()
    assert "_research_jobs.sort(key=lambda j: j[2], reverse=True)" in branch
    assert "_research_jobs = _research_jobs[:_jobs_cap]" in branch


def test_b1_dropped_coin_emits_audit_log_event():
    """Review fix: each dropped coin must be recorded in the event hash-chain."""
    branch = _drop_branch_src()
    assert 'log_event({' in branch
    assert '"event": "ta_skip"' in branch
    assert '"signal": "JOBS_BACKPRESSURE"' in branch
    assert '"reason": "jobs_backpressure_cap"' in branch


def test_b1_dropped_coin_recorded_in_cycle_outcomes():
    branch = _drop_branch_src()
    assert '(_dj_coin, "skip", False, "jobs_backpressure_cap")' in branch


def test_b1_dropped_coin_stamps_all_rolled_back():
    branch = _drop_branch_src()
    # Time/score throttle stamps.
    assert "_last_research_by_coin.pop(_dj_coin, None)" in branch
    assert "_last_research_score_by_coin.pop(_dj_coin, None)" in branch
    # Content-dedup fingerprint: recomputed from the SAME perception object
    # (_dj[1]) and discarded (idempotent), mirroring the enqueue add guard.
    assert "_dj_fp = signal_fingerprint(_dj[1])" in branch
    assert "if _dj_fp is not None:" in branch
    assert "_researched_signal_fps.discard(_dj_fp)" in branch
    # Must NOT use remove() (would KeyError the loop tail if fp absent).
    assert "_researched_signal_fps.remove(" not in branch


def test_b1_order_comments_no_longer_claim_trigger_order_after_trim():
    """Review fix: phase-2/phase-3 ordering comments reflect score-desc trim."""
    # The stale "iterated in the original trigger order" phrasing must be gone.
    assert "iterated in the original trigger order" not in TRADING_LOOP_SRC
    assert "Futures are appended in trigger order" not in TRADING_LOOP_SRC
    # The corrected note must acknowledge score-desc ordering.
    assert "score-desc" in TRADING_LOOP_SRC
