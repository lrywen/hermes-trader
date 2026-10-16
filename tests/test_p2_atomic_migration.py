"""P2 (audit 2026-10-15): non-atomic state writers migrated to shared atomic_io.

Four authoritative state writers used to hand-roll tmp+os.replace with no
fsync: canary mark_decision, selection_attribution _save_state, risk_bypass
_persist, and event_log _write_anchor. These tests pin (a) that each now routes
through atomic_io (the migration), (b) that output is byte-correct and the
JSONL / JSON shape is preserved, and (c) that no stale ``.tmp`` remains.
"""
from __future__ import annotations

import json

from test_canary_pipeline import NOW_MS, _full_evidence

import hermes_trader.agents.atomic_io as atomic_io
import hermes_trader.agents.canary_pipeline as canary
import hermes_trader.agents.risk_bypass as risk_bypass
import hermes_trader.agents.selection_attribution as selattr

# ── selection_attribution._save_state ───────────────────────────────────────

def test_selection_attribution_save_routes_through_atomic(tmp_path, monkeypatch):
    target = str(tmp_path / "selection_attribution.json")
    monkeypatch.setattr(selattr, "_state_path", lambda: target)
    called = []
    orig = atomic_io.locked_write_json_atomic
    monkeypatch.setattr(
        atomic_io, "locked_write_json_atomic",
        lambda *a, **k: (called.append((a, k)), orig(*a, **k))[1],
    )
    selattr._save_state({"items": [{"id": "x", "fwd_pct": 1.2}]})
    assert called, "expected _save_state to call atomic_io.locked_write_json_atomic"
    assert called[0][1]["fsync"] is True
    # byte-correct round-trip
    data = json.loads(target and open(target).read())
    assert data["items"][0]["fwd_pct"] == 1.2
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


# ── risk_bypass._persist ────────────────────────────────────────────────────

def test_risk_bypass_persist_routes_through_atomic(tmp_path, monkeypatch):
    target = str(tmp_path / "risk_bypass.json")
    monkeypatch.setattr(risk_bypass, "_state_path", lambda: target)
    monkeypatch.setattr(risk_bypass, "_audit_path", lambda: str(tmp_path / "audit.jsonl"))
    called = []
    orig = atomic_io.locked_write_json_atomic
    monkeypatch.setattr(
        atomic_io, "locked_write_json_atomic",
        lambda *a, **k: (called.append((a, k)), orig(*a, **k))[1],
    )
    risk_bypass._persist({"armed": True, "expires_epoch": 1})
    assert called and called[0][1]["fsync"] is True
    data = json.loads(open(target).read())
    assert data["armed"] is True
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_risk_bypass_arm_persists_durably(tmp_path, monkeypatch):
    target = str(tmp_path / "risk_bypass.json")
    monkeypatch.setattr(risk_bypass, "_state_path", lambda: target)
    monkeypatch.setattr(risk_bypass, "_audit_path", lambda: str(tmp_path / "audit.jsonl"))
    st = risk_bypass.arm(set_by="operator", ttl_seconds=300, now=1000.0)
    assert st["armed"] is True and st["expires_epoch"] == 1300.0
    data = json.loads(open(target).read())
    assert data["armed"] is True


# ── canary_pipeline.mark_decision ───────────────────────────────────────────

def test_canary_mark_decision_preserves_jsonl_and_routes_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / "pending.jsonl")
    monkeypatch.setenv("HERMES_CANARY_PIPELINE_FILE", path)
    canary.append_record(
        canary.build_pending_record("btc", _full_evidence(), now_ms=NOW_MS))
    records = canary.read_records(path)
    assert records[0]["state"] == canary.PENDING
    called = []
    orig = atomic_io.write_bytes_atomic
    monkeypatch.setattr(
        atomic_io, "write_bytes_atomic",
        lambda *a, **k: (called.append((a, k)), orig(*a, **k))[1],
    )
    ok = canary.mark_decision(arm="btc", state="APPROVED", operator="op",
                              now_ms=2000, path=path)
    assert ok is True
    assert called and called[0][1]["fsync"] is True
    # JSONL shape preserved, one dict per line
    lines = [json.loads(l) for l in open(path).read().splitlines()]
    assert len(lines) == 1
    assert lines[0]["state"] == "APPROVED" and lines[0]["operator"] == "op"
    assert lines[0]["decided_ms"] == 2000
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


# ── event_log._write_anchor ─────────────────────────────────────────────────

def test_event_log_anchor_routes_through_atomic(tmp_path, monkeypatch):
    import hermes_trader.event_log as el
    anchor = tmp_path / "anchor"
    monkeypatch.setattr(el, "_anchor_path", lambda: anchor)
    called = []
    orig = atomic_io.write_bytes_atomic
    monkeypatch.setattr(
        atomic_io, "write_bytes_atomic",
        lambda *a, **k: (called.append((a, k)), orig(*a, **k))[1],
    )
    el._write_anchor(42, "abc123")
    assert called and called[0][1]["fsync"] is True
    data = json.loads(anchor.read_text(encoding="utf-8"))
    assert data["seq"] == 42 and data["hash"] == "abc123"
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


# ── event_log.append fsyncs each line ───────────────────────────────────────

def test_event_log_append_writes_chained_line(tmp_path, monkeypatch):
    import hermes_trader.event_log as el
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(el, "EVENTS_FILE", str(ev_file))
    monkeypatch.setattr(el, "_MAX_EVENTS_BYTES", 10**9)  # no rotation in test
    anchor = tmp_path / "anchor"
    monkeypatch.setattr(el, "_anchor_path", lambda: anchor)
    # the append fd must be fsynced (flush-only previously lost lines on crash)
    import os
    fsynced_fds = []
    orig_fsync = os.fsync
    monkeypatch.setattr(
        os, "fsync",
        lambda fd: (fsynced_fds.append(fd), orig_fsync(fd))[1],
    )
    ok = el.append("test_event", {"k": "v"}, trace_id="T")
    assert ok is True
    assert ev_file.exists()
    line = json.loads(ev_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["event"] == "test_event" and line["trace_id"] == "T"
    assert "prev_hash" in line  # chained (first record anchors to genesis)
    assert "hash" in line and line["seq"] >= 1
    assert fsynced_fds  # append() called os.fsync on the events fd
