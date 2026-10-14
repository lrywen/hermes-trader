"""R4 tests: semi-automated canary promotion pipeline.

The pipeline prepares PENDING_APPROVAL records when every multi-dimensional
gate clears; it must never auto-authorize LIVE. These tests pin gate
evaluation, pending-record construction, dedup, and the operator decision.
"""
from __future__ import annotations

import json

from hermes_trader.agents import canary_pipeline as cp

# Fixed evaluation instant for staleness math.
NOW_MS = 1_800_000_000_000  # arbitrary fixed epoch-ms
DAY_MS = 86_400_000


def _full_evidence(**over):
    ev = {
        "mature_outcomes": 80,
        "independent_days": 25,
        "net_edge_bps": 8.0,
        "bb_ci_lo_bps": 5.0,            # clear of the borderline band
        "dsr_p": 0.97,
        "pbo": 0.3,
        "wfr": 0.62,
        "realistic_fill_positive": True,
        "killswitch_tested": True,
        "orderable": True,
        # P2-a: newest observation is 2 days old (fresh vs 14-day limit).
        "last_record_ms": NOW_MS - 2 * DAY_MS,
    }
    ev.update(over)
    return ev


def test_all_gates_pass_with_full_evidence():
    res = cp.evaluate_gates(_full_evidence(), now_ms=NOW_MS)
    assert res["passed"] is True
    assert all(c["passed"] for c in res["checks"])
    assert res["borderline"] == []


def test_missing_dimension_fails_closed():
    ev = _full_evidence()
    del ev["dsr_p"]
    res = cp.evaluate_gates(ev, now_ms=NOW_MS)
    assert res["passed"] is False
    failed = {c["gate"] for c in res["checks"] if not c["passed"]}
    assert "deflated_sharpe" in failed


def test_weak_edge_rejected():
    res = cp.evaluate_gates(_full_evidence(net_edge_bps=2.0), now_ms=NOW_MS)
    assert res["passed"] is False


def test_wfr_must_exceed_threshold():
    # exactly at the threshold does NOT clear
    res = cp.evaluate_gates(_full_evidence(wfr=0.5), now_ms=NOW_MS)
    assert res["passed"] is False
    res2 = cp.evaluate_gates(_full_evidence(wfr=0.51), now_ms=NOW_MS)
    assert res2["passed"] is True


def test_build_pending_record_raises_when_unqualified():
    import pytest
    with pytest.raises(ValueError):
        cp.build_pending_record("x", _full_evidence(dsr_p=0.5), now_ms=NOW_MS)


def test_pending_record_is_inert():
    rec = cp.build_pending_record("signal_age_decay", _full_evidence(),
                                  now_ms=NOW_MS)
    assert rec["state"] == cp.PENDING
    assert rec["operator"] is None and rec["decided_ms"] is None
    assert rec["arm"] == "signal_age_decay"


# ── P2 evidence-discipline gates ────────────────────────────────────────────

def test_stale_evidence_fails_but_recent_passes():
    # 20 days old > 14-day limit → fail.
    old = _full_evidence(last_record_ms=NOW_MS - 20 * DAY_MS)
    res = cp.evaluate_gates(old, now_ms=NOW_MS)
    assert res["passed"] is False
    assert not next(c for c in res["checks"]
                    if c["gate"] == "evidence_freshness")["passed"]
    # exactly at the 14-day boundary passes (<=).
    boundary = _full_evidence(last_record_ms=NOW_MS - 14 * DAY_MS)
    assert cp.evaluate_gates(boundary, now_ms=NOW_MS)["passed"] is True


def test_missing_last_record_ms_fails_closed():
    ev = _full_evidence()
    del ev["last_record_ms"]
    res = cp.evaluate_gates(ev, now_ms=NOW_MS)
    assert res["passed"] is False
    assert not next(c for c in res["checks"]
                    if c["gate"] == "evidence_freshness")["passed"]


def test_breakeven_fee_cushion():
    # net edge 6.0 exactly meets both floors → pass; just below → fail.
    assert cp.evaluate_gates(
        _full_evidence(net_edge_bps=6.0), now_ms=NOW_MS)["passed"] is True
    res = cp.evaluate_gates(
        _full_evidence(net_edge_bps=5.9), now_ms=NOW_MS)
    assert not next(c for c in res["checks"]
                    if c["gate"] == "economic_thickness")["passed"]
    # a stricter configured breakeven raises the bar.
    res2 = cp.evaluate_gates(
        _full_evidence(net_edge_bps=8.0),
        gates={"breakeven_fee_bps": 10.0}, now_ms=NOW_MS)
    assert res2["passed"] is False


def test_borderline_ci_marker_is_non_fatal():
    # CI lower bound clears 0 but is inside (0, 3) → still passes, flagged.
    res = cp.evaluate_gates(
        _full_evidence(bb_ci_lo_bps=1.5), now_ms=NOW_MS)
    assert res["passed"] is True
    assert res["borderline"] and res["borderline"][0]["dimension"] == "bootstrap_ci"
    # a robust CI (>= band) carries no marker.
    res2 = cp.evaluate_gates(
        _full_evidence(bb_ci_lo_bps=4.0), now_ms=NOW_MS)
    assert res2["borderline"] == []
    # CI exactly 0 fails the hard gate (strict >0) and yields no marker.
    res3 = cp.evaluate_gates(
        _full_evidence(bb_ci_lo_bps=0.0), now_ms=NOW_MS)
    assert res3["passed"] is False and res3["borderline"] == []


def test_pending_record_carries_borderline_marker():
    rec = cp.build_pending_record(
        "signal_age_decay", _full_evidence(bb_ci_lo_bps=1.0), now_ms=NOW_MS)
    assert rec["state"] == cp.PENDING
    assert rec["borderline"][0]["dimension"] == "bootstrap_ci"


def test_append_dedup(tmp_path, monkeypatch):
    path = str(tmp_path / "pending.jsonl")
    monkeypatch.setenv("HERMES_CANARY_PIPELINE_FILE", path)
    rec = cp.build_pending_record("a", _full_evidence(), config_sha256="h1",
                                  now_ms=NOW_MS)
    assert cp.append_record(rec) is True
    # same arm + same config -> deduped
    rec2 = cp.build_pending_record("a", _full_evidence(), config_sha256="h1",
                                   now_ms=NOW_MS)
    assert cp.append_record(rec2) is False
    # different config -> queued
    rec3 = cp.build_pending_record("a", _full_evidence(), config_sha256="h2",
                                   now_ms=NOW_MS)
    assert cp.append_record(rec3) is True
    lines = path and open(path).read().splitlines()
    assert len(lines) == 2


def test_operator_decision_flips_state(tmp_path, monkeypatch):
    path = str(tmp_path / "pending.jsonl")
    monkeypatch.setenv("HERMES_CANARY_PIPELINE_FILE", path)
    cp.append_record(cp.build_pending_record("a", _full_evidence(),
                                             now_ms=NOW_MS))
    assert cp.mark_decision("a", cp.APPROVED, operator="alice") is True
    rows = [json.loads(l) for l in open(path).read().splitlines()]
    assert rows[0]["state"] == cp.APPROVED
    assert rows[0]["operator"] == "alice"
    # no further pending record to decide
    assert cp.mark_decision("a", cp.REJECTED, operator="bob") is False


def test_decision_rejects_bad_state():
    import pytest
    with pytest.raises(ValueError):
        cp.mark_decision("a", "LIVE", operator="x")
