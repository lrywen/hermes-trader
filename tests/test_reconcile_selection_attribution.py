"""Tests for the daily selection-attribution verdict script.

The verdict only reads the (already-settled) attribution summary; these tests
stub ``attribution_summary`` to feed each gate combination and assert the
PROMOTE/DEMOTE/HOLD decision. The script never changes mode, which is covered
by reading main() with --write against a tmp data dir.
"""
from __future__ import annotations

import json

import pytest

from scripts import reconcile_selection_attribution as rsa
from scripts.reconcile_selection_attribution import GATES


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(rsa.sa, "settle_due", lambda *a, **k: 0)
    return tmp_path


def _stub(monkeypatch, n_pairs, diff, p):
    monkeypatch.setattr(
        rsa.sa, "attribution_summary",
        lambda source: {
            "source": source, "n_selected": n_pairs, "n_deferred": n_pairs,
            "mean_selected_pct": diff, "mean_deferred_pct": 0.0,
            "mean_diff_pct": diff, "n_pairs": n_pairs, "p_value": p})


def test_promote_when_all_gates_clear(data_dir, monkeypatch):
    _stub(monkeypatch, n_pairs=30, diff=1.5, p=0.01)
    v = rsa._verdict_for("signal_ranking")
    assert v["verdict"] == "PROMOTE"


def test_hold_when_sample_too_small(data_dir, monkeypatch):
    _stub(monkeypatch, n_pairs=20, diff=1.5, p=0.01)
    v = rsa._verdict_for("signal_ranking")
    assert v["verdict"] == "HOLD"


def test_hold_when_not_significant(data_dir, monkeypatch):
    _stub(monkeypatch, n_pairs=30, diff=1.5, p=0.2)
    v = rsa._verdict_for("signal_ranking")
    assert v["verdict"] == "HOLD"


def test_demote_when_no_edge(data_dir, monkeypatch):
    _stub(monkeypatch, n_pairs=30, diff=-0.5, p=0.01)
    v = rsa._verdict_for("coin_selection")
    assert v["verdict"] == "DEMOTE"


def test_demote_at_zero_diff(data_dir, monkeypatch):
    _stub(monkeypatch, n_pairs=30, diff=0.0, p=0.01)
    assert rsa._verdict_for("coin_selection")["verdict"] == "DEMOTE"


def test_unstucking_holds_before_checkpoint(data_dir, monkeypatch):
    # Freeze "today" before 2026-10-16 -> always HOLD regardless of records.
    import time as _t
    monkeypatch.setattr(rsa.time, "strftime",
                        lambda fmt, gmtime=None: "2026-10-10")
    v = rsa._unstucking_verdict()
    assert v["verdict"] == "HOLD"
    assert "2026-10-16" in v["reason"]


def test_unstucking_demotes_when_evidence_absent_after_checkpoint(
        data_dir, monkeypatch):
    monkeypatch.setattr(rsa.time, "strftime",
                        lambda fmt, gmtime=None: "2026-10-16")
    # No unstucking file in the tmp dir -> 0 non-empty -> DEMOTE.
    v = rsa._unstucking_verdict()
    assert v["verdict"] == "DEMOTE"


def test_unstucking_holds_with_some_nonempty(data_dir, monkeypatch):
    monkeypatch.setattr(rsa.time, "strftime",
                        lambda fmt, gmtime=None: "2026-10-16")
    recs = "\n".join(
        json.dumps({"selected": [{"coin": "X"}]}) for _ in range(6))
    (data_dir / "unstucking.jsonl").write_text(recs)
    v = rsa._unstucking_verdict()
    # 6 non-empty is >=5 (not demoted) but <15 (not promoted) -> HOLD.
    assert v["verdict"] == "HOLD"


def test_main_write_persists_verdicts(data_dir, monkeypatch):
    _stub(monkeypatch, n_pairs=30, diff=1.5, p=0.01)
    monkeypatch.setattr(rsa.time, "strftime",
                        lambda fmt, gmtime=None: "2026-10-10")
    rsa.main(["--write"])
    out = data_dir / "selection_attribution_verdicts.jsonl"
    payload = json.loads(out.read_text())
    sources = {v["source"] for v in payload["verdicts"]}
    assert {"signal_ranking", "coin_selection", "unstucking"} <= sources
