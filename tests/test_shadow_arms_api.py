"""Tests for the M1 shadow-arm grading center (Audit 2026-09-07).

Covers the three new dashboard endpoints plus the nightly grade-history
snapshotting and the blind-gate SSE mirror:

  * scripts/shadow_grade.py: append_history / read_history / _slim_snapshot /
    _trim_history (best-effort, never raises),
  * dashboard_routes/shadow_arms.py: GET grades (TTL cached, anonymous-safe),
    POST refresh (operator-gated, audited), GET grade-history, 422 on bad
    windows, 503 when the grader script is unavailable,
  * agents/risk_gates.py: _alert_memory_gate_blind also emits a
    ``risk_gate_blind`` session-log (SSE) event while still never raising.
"""
import importlib.util
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.dashboard import register_routes

_OP_TOKEN = "test-op-secret-m1"
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")
_GRADE_PATH = os.path.join(_SCRIPTS, "shadow_grade.py")


def _load_grade():
    spec = importlib.util.spec_from_file_location("shadow_grade_m1_under_test", _GRADE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sg():
    return _load_grade()


# ── grade-history snapshotting (scripts/shadow_grade.py) ──────────────────────

def _fake_report():
    return {
        "generated_at": "2026-09-07 00:45 UTC",
        "windows_h": [24, 72, 168],
        "arms": [
            {"arm": "ta_late_entry", "mode": "shadow", "kind": "block",
             "verdict": "DATA_GAP",
             "windows": [
                 {"window_h": 24, "total": 0, "hits": 0, "decisions": 0,
                  "hit_rate": 0.0, "mature_outcomes": 0},
                 {"window_h": 72, "total": 0, "hits": 0, "decisions": 0,
                  "hit_rate": 0.0, "mature_outcomes": 0},
                 {"window_h": 168, "total": 0, "hits": 0, "decisions": 0,
                  "hit_rate": 0.0, "mature_outcomes": 0},
             ]},
            {"arm": "sizing_v2", "mode": "shadow", "kind": "change",
             "verdict": "PROMOTE_CANDIDATE",
             "windows": [
                 {"window_h": 24, "total": 10, "hits": 8, "decisions": 10,
                  "hit_rate": 0.8, "mature_outcomes": 0},
                 {"window_h": 72, "total": 30, "hits": 24, "decisions": 30,
                  "hit_rate": 0.8, "mature_outcomes": 0},
                 {"window_h": 168, "total": 70, "hits": 54, "decisions": 70,
                  "hit_rate": 0.77, "mature_outcomes": 0},
             ]},
        ],
        "real_baseline": {"real_closes": 0, "real_win_rate": None, "note": ""},
    }


def test_slim_snapshot_projects_longest_window(sg):
    snap = sg._slim_snapshot(_fake_report())
    assert snap["window_h"] == 168
    assert snap["real_closes"] == 0
    by_arm = {a["arm"]: a for a in snap["arms"]}
    assert by_arm["ta_late_entry"]["verdict"] == "DATA_GAP"
    assert by_arm["ta_late_entry"]["total"] == 0
    sizing = by_arm["sizing_v2"]
    assert sizing["verdict"] == "PROMOTE_CANDIDATE"
    # flat longest-window fields are retained for old readers...
    assert sizing["total"] == 70 and sizing["hits"] == 54
    assert sizing["hit_rate"] == 0.77
    # ...and CS-C window-scoped history keeps ALL three windows per arm so the
    # 24h/72h trend survives the nightly snapshot.
    windows = {w["window_h"]: w for w in sizing["windows"]}
    assert sorted(windows) == [24, 72, 168]
    assert windows[24]["total"] == 10 and windows[24]["hit_rate"] == 0.8
    assert windows[72]["total"] == 30
    assert windows[168]["total"] == 70 and windows[168]["hits"] == 54


def test_append_and_read_history_roundtrip(sg, tmp_path):
    hist = tmp_path / "grade_history.jsonl"
    assert sg.append_history(_fake_report(), path=str(hist)) is True
    rows = sg.read_history(path=str(hist))
    assert len(rows) == 1
    assert rows[0]["arms"][0]["arm"] == "ta_late_entry"
    assert isinstance(rows[0]["ts"], int)


def test_read_history_missing_file_returns_empty(sg, tmp_path):
    assert sg.read_history(path=str(tmp_path / "nope.jsonl")) == []


def test_read_history_since_filter(sg, tmp_path):
    hist = tmp_path / "h.jsonl"
    old = sg._slim_snapshot(_fake_report())
    old["ts"] = 1_000
    new = sg._slim_snapshot(_fake_report())
    new["ts"] = 2_000
    with open(hist, "w", encoding="utf-8") as fh:
        import json
        fh.write(json.dumps(old) + "\n")
        fh.write(json.dumps(new) + "\n")
    rows = sg.read_history(path=str(hist), since_ms=1_500)
    assert len(rows) == 1 and rows[0]["ts"] == 2_000


def test_trim_history_caps_lines(sg, tmp_path):
    hist = tmp_path / "h.jsonl"
    sg.HISTORY_MAX_LINES = 3
    import json
    for i in range(5):
        snap = sg._slim_snapshot(_fake_report())
        snap["ts"] = i
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap) + "\n")
    sg._trim_history(str(hist))
    rows = sg.read_history(path=str(hist))
    assert len(rows) == 3
    assert [r["ts"] for r in rows] == [2, 3, 4]  # newest kept, oldest dropped


def test_append_history_bad_path_never_raises(sg):
    # An unwritable path must swallow the error and return False.
    assert sg.append_history(_fake_report(), path="/nonexistent-dir-xyz/h.jsonl") is False


# ── dashboard endpoints (dashboard_routes/shadow_arms.py) ────────────────────

class _StubGrader:
    """Fake shadow_grade module — keeps endpoint tests independent of /data."""

    def __init__(self):
        self.grade_calls = 0
        self.history_calls = 0

    def collect_grades(self, windows):
        self.grade_calls += 1
        return {
            "generated_at": "2026-09-07 00:45 UTC",
            "windows_h": list(windows),
            "arms": [
                {"arm": "ta_late_entry", "mode": "shadow", "kind": "block",
                 "verdict": "DATA_GAP", "verdict_cn": "采数缺口",
                 "reason": "0 条", "windows": []},
                {"arm": "sizing_v2", "mode": "shadow", "kind": "change",
                 "verdict": "PROMOTE_CANDIDATE", "verdict_cn": "可升级",
                 "reason": "健康", "windows": []},
            ],
            "real_baseline": {"real_closes": 0, "real_win_rate": None, "note": ""},
        }

    def read_history(self, since_ms=None, limit=365):
        self.history_calls += 1
        return [{"ts": 1_700_000_000_000, "arms": [{"arm": "x", "verdict": "OFF"}]}]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    from hermes_trader import dashboard
    from hermes_trader.dashboard_routes import shadow_arms
    stub = _StubGrader()
    monkeypatch.setattr(shadow_arms, "_load_shadow_grade", lambda: stub)
    dashboard._TTL_CACHE.clear()
    app = FastAPI()
    register_routes(app)
    return TestClient(app, raise_server_exceptions=False), stub


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def test_grades_endpoint_anonymous_safe_and_cached(client):
    c, stub = client
    r = c.get("/api/dashboard/shadow-arms/grades")
    assert r.status_code == 200
    data = r.json()
    assert {a["arm"] for a in data["arms"]} == {"ta_late_entry", "sizing_v2"}
    assert data["cache_ttl_s"] == 60.0
    # Second call within TTL must be served from cache (loader not re-invoked).
    r2 = c.get("/api/dashboard/shadow-arms/grades")
    assert r2.status_code == 200
    assert stub.grade_calls == 1


def test_grades_endpoint_bad_window_422(client):
    c, _ = client
    r = c.get("/api/dashboard/shadow-arms/grades?windows=24,banana")
    assert r.status_code == 422


def test_grade_history_endpoint(client):
    c, stub = client
    r = c.get("/api/dashboard/shadow-arms/grade-history?days=7")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["snapshots"][0]["arms"][0]["verdict"] == "OFF"
    assert stub.history_calls == 1


def test_refresh_requires_operator_token(client):
    c, _ = client
    r = c.post("/api/dashboard/shadow-arms/refresh", json={})
    assert r.status_code == 401


def test_refresh_with_token_regrades_and_warms_cache(client):
    c, stub = client
    r = c.post("/api/dashboard/shadow-arms/refresh", json={}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert stub.grade_calls == 1
    # Cache was warmed: a subsequent GET must not invoke the loader again.
    r2 = c.get("/api/dashboard/shadow-arms/grades")
    assert r2.status_code == 200
    assert stub.grade_calls == 1


def test_refresh_audits_session_log(client, monkeypatch):
    c, _ = client
    captured = []
    from hermes_trader import session_log
    monkeypatch.setattr(session_log, "append",
                        lambda ev: captured.append(ev) if ev.get("event") == "shadow_arms_refresh" else None)
    r = c.post("/api/dashboard/shadow-arms/refresh", json={}, headers=_auth())
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0]["via"] == "web"
    assert captured[0]["counts"].get("DATA_GAP") == 1


def test_endpoint_503_when_grader_unavailable(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    from hermes_trader import dashboard
    from hermes_trader.dashboard_routes import shadow_arms
    monkeypatch.setattr(shadow_arms, "_load_shadow_grade",
                        lambda: (_ for _ in ()).throw(RuntimeError("grader missing")))
    dashboard._TTL_CACHE.clear()
    app = FastAPI()
    register_routes(app)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/dashboard/shadow-arms/grades").status_code == 503
    assert c.get("/api/dashboard/shadow-arms/grade-history").status_code == 503
    r = c.post("/api/dashboard/shadow-arms/refresh", json={}, headers=_auth())
    assert r.status_code == 503


# ── blind-gate SSE mirror (agents/risk_gates.py) ─────────────────────────────

def test_alert_memory_gate_blind_emits_sse_event(monkeypatch):
    from hermes_trader.agents import risk_gates
    from hermes_trader import session_log
    captured = []
    monkeypatch.setattr(session_log, "append", lambda ev: captured.append(ev))
    # notify.send_card must be invoked but must not matter if it raises.
    monkeypatch.setattr("hermes_trader.notify.send_card",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("feishu down")))
    risk_gates._alert_memory_gate_blind("coin_circuit", None, RuntimeError("state read fail"))
    assert len(captured) == 1
    ev = captured[0]
    assert ev["event"] == "risk_gate_blind"
    assert ev["gate"] == "coin_circuit"
    assert ev["posture"] == "fail-open"
    assert "state read fail" in ev["error"]
    assert ev["coin"] == "-"


def test_alert_memory_gate_blind_never_raises(monkeypatch):
    from hermes_trader.agents import risk_gates
    from hermes_trader import session_log
    # Even if the session-log append itself explodes, the hot path is safe.
    monkeypatch.setattr(session_log, "append",
                        lambda ev: (_ for _ in ()).throw(RuntimeError("log disk dead")))
    risk_gates._alert_memory_gate_blind("global_halt", None, ValueError("boom"))
