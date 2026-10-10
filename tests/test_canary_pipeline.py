"""R4 tests: semi-automated canary promotion pipeline.

The pipeline prepares PENDING_APPROVAL records when every multi-dimensional
gate clears; it must never auto-authorize LIVE. These tests pin gate
evaluation, pending-record construction, dedup, and the operator decision.
"""
from __future__ import annotations

import json

from hermes_trader.agents import canary_pipeline as cp


def _full_evidence(**over):
    ev = {
        "mature_outcomes": 80,
        "independent_days": 25,
        "net_edge_bps": 8.0,
        "bb_ci_lo_bps": 2.0,
        "dsr_p": 0.97,
        "pbo": 0.3,
        "wfr": 0.62,
        "realistic_fill_positive": True,
        "killswitch_tested": True,
        "orderable": True,
    }
    ev.update(over)
    return ev


def test_all_gates_pass_with_full_evidence():
    res = cp.evaluate_gates(_full_evidence())
    assert res["passed"] is True
    assert all(c["passed"] for c in res["checks"])


def test_missing_dimension_fails_closed():
    ev = _full_evidence()
    del ev["dsr_p"]
    res = cp.evaluate_gates(ev)
    assert res["passed"] is False
    failed = {c["gate"] for c in res["checks"] if not c["passed"]}
    assert "deflated_sharpe" in failed


def test_weak_edge_rejected():
    res = cp.evaluate_gates(_full_evidence(net_edge_bps=2.0))
    assert res["passed"] is False


def test_wfr_must_exceed_threshold():
    # exactly at the threshold does NOT clear
    res = cp.evaluate_gates(_full_evidence(wfr=0.5))
    assert res["passed"] is False
    res2 = cp.evaluate_gates(_full_evidence(wfr=0.51))
    assert res2["passed"] is True


def test_build_pending_record_raises_when_unqualified():
    import pytest
    with pytest.raises(ValueError):
        cp.build_pending_record("x", _full_evidence(dsr_p=0.5))


def test_pending_record_is_inert():
    rec = cp.build_pending_record("signal_age_decay", _full_evidence())
    assert rec["state"] == cp.PENDING
    assert rec["operator"] is None and rec["decided_ms"] is None
    assert rec["arm"] == "signal_age_decay"


def test_append_dedup(tmp_path, monkeypatch):
    path = str(tmp_path / "pending.jsonl")
    monkeypatch.setenv("HERMES_CANARY_PIPELINE_FILE", path)
    rec = cp.build_pending_record("a", _full_evidence(), config_sha256="h1")
    assert cp.append_record(rec) is True
    # same arm + same config -> deduped
    rec2 = cp.build_pending_record("a", _full_evidence(), config_sha256="h1")
    assert cp.append_record(rec2) is False
    # different config -> queued
    rec3 = cp.build_pending_record("a", _full_evidence(), config_sha256="h2")
    assert cp.append_record(rec3) is True
    lines = path and open(path).read().splitlines()
    assert len(lines) == 2


def test_operator_decision_flips_state(tmp_path, monkeypatch):
    path = str(tmp_path / "pending.jsonl")
    monkeypatch.setenv("HERMES_CANARY_PIPELINE_FILE", path)
    cp.append_record(cp.build_pending_record("a", _full_evidence()))
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
