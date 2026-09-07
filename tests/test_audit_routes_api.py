"""Tests for the M4 audit-ledger routes (Audit 2026-09-07).

Covers the three new dashboard endpoints plus the reconcile status writer:

  * dashboard_routes/audit.py: GET ledger/verify (hash-chain replay over a
    real chained log; tamper and corrupt-line detection), GET ledger/events
    (event_type filter, limit=newest-N slicing, _dt stripped, ascending),
    GET reconcile/status (404 when never run, 503 on unparseable file, 200
    passthrough),
  * scripts/reconcile_fills.py: build_status / write_status slim payload +
    atomic roundtrip, best-effort failure never raises.
"""
import importlib.util
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader import event_log
from hermes_trader.dashboard_routes import audit as audit_mod
from hermes_trader.dashboard_routes.audit import register_audit_routes

_OP_TOKEN = "test-op-secret-m4"
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")
_RECONCILE_PATH = os.path.join(_SCRIPTS, "reconcile_fills.py")


def _load_reconcile():
    spec = importlib.util.spec_from_file_location(
        "reconcile_fills_m4_under_test", _RECONCILE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def events_file(tmp_path, monkeypatch):
    """Redirect events.jsonl at a fresh tmp file and reset the chain anchor."""
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(path))
    return str(path)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    app = FastAPI()
    register_audit_routes(app)
    return TestClient(app, raise_server_exceptions=False)


# ── ledger/verify ────────────────────────────────────────────────────────────

def test_verify_clean_chain(client, events_file):
    for i in range(3):
        assert event_log.append("order", {"n": i}, trace_id=f"t{i}") is True
    r = client.get("/api/dashboard/ledger/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["chained_records"] == 3
    assert data["last_seq"] == 3
    assert data["errors"] == []
    assert data["checked_at"]


def test_verify_detects_tampered_record(client, events_file):
    for i in range(3):
        event_log.append("order", {"px": 100 + i})
    # Tamper the middle record's payload without touching its hash.
    with open(events_file, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    lines[1]["payload"]["px"] = 9999
    with open(events_file, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec) + "\n")
    r = client.get("/api/dashboard/ledger/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    reasons = {e["reason"] for e in data["errors"]}
    assert "hash_mismatch" in reasons


def test_verify_detects_corrupt_line(client, events_file):
    event_log.append("order", {"px": 1})
    with open(events_file, "a", encoding="utf-8") as f:
        f.write("{this is not valid json\n")
    r = client.get("/api/dashboard/ledger/verify")
    data = r.json()
    assert data["ok"] is False
    assert data["corrupt_lines"] >= 1
    assert any(e["reason"] == "unparseable_json" for e in data["errors"])


# ── ledger/events ────────────────────────────────────────────────────────────

def test_events_ascending_and_strips_internal_dt(client, events_file):
    for i, ts in enumerate([
        "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z",
        "2026-09-03T00:00:00Z",
    ]):
        event_log.append("order", {"i": i}, timestamp=ts)
    r = client.get("/api/dashboard/ledger/events")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 3
    assert data["limited"] is False
    ts = [e["timestamp"] for e in data["events"]]
    assert ts == sorted(ts)
    assert all("_dt" not in e for e in data["events"])


def test_events_event_type_filter(client, events_file):
    event_log.append("order", {"x": 1}, timestamp="2026-09-01T00:00:00Z")
    event_log.append("close", {"x": 2}, timestamp="2026-09-02T00:00:00Z")
    r = client.get("/api/dashboard/ledger/events?event_type=close")
    data = r.json()
    assert data["count"] == 1
    assert data["events"][0]["event"] == "close"


def test_events_limit_keeps_newest(client, events_file):
    for i in range(5):
        event_log.append("order", {"i": i},
                         timestamp=f"2026-09-0{i+1}T00:00:00Z")
    r = client.get("/api/dashboard/ledger/events?limit=2")
    data = r.json()
    assert data["count"] == 2
    assert data["limited"] is True
    assert data["total_scanned"] == 5
    # Ascending order, newest two retained: i == 3 and i == 4.
    assert [e["payload"]["i"] for e in data["events"]] == [3, 4]


def test_events_limit_validation(client, events_file):
    assert client.get("/api/dashboard/ledger/events?limit=0").status_code == 422
    assert client.get("/api/dashboard/ledger/events?limit=501").status_code == 422


# ── reconcile/status ─────────────────────────────────────────────────────────

def test_reconcile_status_404_when_never_run(client, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "RECONCILE_STATUS_FILE",
                        str(tmp_path / "missing.json"))
    r = client.get("/api/dashboard/reconcile/status")
    assert r.status_code == 404


def test_reconcile_status_503_on_unparseable(client, tmp_path, monkeypatch):
    bad = tmp_path / "status.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(audit_mod, "RECONCILE_STATUS_FILE", str(bad))
    r = client.get("/api/dashboard/reconcile/status")
    assert r.status_code == 503


def test_reconcile_status_passthrough(client, tmp_path, monkeypatch):
    status = {
        "generated_at": "2026-09-07T00:15:00Z",
        "window_hours": 26.0,
        "status": "clean",
        "issues_total": 0,
        "orphan_opens_count": 0,
        "orphan_closes_count": 0,
        "phantom_closes_count": 0,
    }
    f = tmp_path / "status.json"
    f.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(audit_mod, "RECONCILE_STATUS_FILE", str(f))
    r = client.get("/api/dashboard/reconcile/status")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "clean"
    assert data["window_hours"] == 26.0
    assert data["issues_total"] == 0


# ── scripts/reconcile_fills.py status writer ─────────────────────────────────

def _fake_report(issues=0):
    orphan_open = [{"coin": "BTC", "side": "B", "px": "50000", "sz": "0.1",
                    "oid": 111, "time": 1700000000000, "closedPnl": "0",
                    "noise": "drop-me"}] if issues else []
    orphan_close = [{"coin": "PURR", "side": "A", "px": "0.5", "sz": "100",
                     "oid": 222, "time": 1700000100000, "closedPnl": "12.3",
                     "noise": "drop-me"}] if issues else []
    phantom = [{"coin": "ETH", "side": "long", "exit_px": 3000.0,
                "close_source": "reconcile_backfill",
                "closed_at": 1700000200000, "close_oid": 333,
                "noise": "drop-me"}] if issues > 1 else []
    return {
        "user": "0xabc", "window_hours": 26.0,
        "exchange_fills_total": 50, "exchange_fills_in_window": 10,
        "exchange_opens": 6, "exchange_closes": 4,
        "local_trades": 6, "local_closes": 4,
        "orphan_opens": orphan_open, "orphan_closes": orphan_close,
        "phantom_closes": phantom,
    }


def test_build_status_clean_and_slim():
    rf = _load_reconcile()
    s = rf.build_status(_fake_report(issues=0))
    assert s["status"] == "clean"
    assert s["issues_total"] == 0
    assert s["generated_at"]
    assert s["orphan_opens"] == [] and s["orphan_closes"] == []


def test_build_status_discrepancies_counts_and_fields():
    rf = _load_reconcile()
    s = rf.build_status(_fake_report(issues=2), backfilled=1)
    assert s["status"] == "discrepancies"
    assert s["orphan_opens_count"] == 1
    assert s["orphan_closes_count"] == 1
    assert s["phantom_closes_count"] == 1
    assert s["issues_total"] == 3
    assert s["backfilled_closes"] == 1
    # Raw exchange noise fields must be projected away.
    assert "noise" not in s["orphan_opens"][0]
    assert s["orphan_closes"][0]["coin"] == "PURR"
    assert s["phantom_closes"][0]["close_oid"] == 333


def test_write_status_roundtrip(tmp_path):
    rf = _load_reconcile()
    status_file = tmp_path / "reconcile_status.json"
    rf.STATUS_FILE = str(status_file)
    assert rf.write_status(_fake_report(issues=2), backfilled=1) is True
    on_disk = json.loads(status_file.read_text(encoding="utf-8"))
    assert on_disk["status"] == "discrepancies"
    assert on_disk["issues_total"] == 3
    assert on_disk["orphan_closes"][0]["oid"] == 222
    # No temp file left behind after the atomic rename.
    assert not (tmp_path / "reconcile_status.json.tmp").exists()


def test_write_status_bad_path_never_raises():
    rf = _load_reconcile()
    rf.STATUS_FILE = "/nonexistent-dir-xyz/reconcile_status.json"
    assert rf.write_status(_fake_report()) is False
