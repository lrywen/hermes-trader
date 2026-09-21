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
import json as _json
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
        fh.write(_json.dumps(old) + "\n")
        fh.write(_json.dumps(new) + "\n")
    rows = sg.read_history(path=str(hist), since_ms=1_500)
    assert len(rows) == 1 and rows[0]["ts"] == 2_000


def test_trim_history_caps_lines(sg, tmp_path):
    hist = tmp_path / "h.jsonl"
    sg.HISTORY_MAX_LINES = 3
    for i in range(5):
        snap = sg._slim_snapshot(_fake_report())
        snap["ts"] = i
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(snap) + "\n")
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

    def read_history(self, since_ms=None, limit=365, source=None):
        self.history_calls += 1
        self.last_source = source
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
    assert stub.last_source is None


def test_grade_history_source_filter_passthrough(client):
    # M9：趋势视图只看 cron 快照，source 参数透传到 read_history
    c, stub = client
    r = c.get("/api/dashboard/shadow-arms/grade-history?days=30&source=cron")
    assert r.status_code == 200
    assert stub.last_source == "cron"


def test_grade_history_bad_source_422(client):
    c, _ = client
    r = c.get("/api/dashboard/shadow-arms/grade-history?source=everything")
    assert r.status_code == 422


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


# ── backfill summary (historical backtest evidence surface) ──────────────────


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(_json.dumps(r) + "\n")


@pytest.fixture()
def make_backfill_client(tmp_path, monkeypatch):
    """Factory for a TestClient wired to HERMES_BACKFILL_DIR=tmp_path with the
    grader stubbed and the TTL cache cleared. Callers write artifact files
    into tmp_path first, then call the factory to build the client."""
    def _make():
        monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
        monkeypatch.setenv("HERMES_BACKFILL_DIR", str(tmp_path))
        from hermes_trader import dashboard
        from hermes_trader.dashboard_routes import shadow_arms
        monkeypatch.setattr(shadow_arms, "_load_shadow_grade", lambda: _StubGrader())
        dashboard._TTL_CACHE.clear()
        app = FastAPI()
        register_routes(app)
        return TestClient(app, raise_server_exceptions=False)
    return _make


@pytest.fixture()
def backfill_client(tmp_path, make_backfill_client):
    _write_jsonl(tmp_path / "ta_late_entry_shadow.backfill.jsonl", [
        {"side": "long", "outcome": "win", "pnl_pct": 4.0},
        {"side": "long", "outcome": "loss", "pnl_pct": -2.0},
        {"side": "short", "outcome": "win", "pnl_pct": 3.0},
    ])
    _write_jsonl(tmp_path / "xs_reversal_shadow.backfill.jsonl", [
        {"is_candidate": True, "forward": {"fwd24h_pct": -8.0, "fwd72h_pct": -7.0}},
        {"is_candidate": False, "forward": {"fwd24h_pct": -4.0, "fwd72h_pct": None}},
    ])
    _write_jsonl(tmp_path / "atr_regime_calib_shadow.backfill.jsonl", [
        {"would_change": True, "cf_v1_pnl_pct": -3.0, "cf_v2_pnl_pct": -3.6,
         "pnl_pct": 0.6, "outcome": "win"},
        {"would_change": True, "cf_v1_pnl_pct": -2.0, "cf_v2_pnl_pct": -1.8,
         "pnl_pct": -0.2, "outcome": "loss"},
        {"would_change": False, "cf_v1_pnl_pct": 1.0, "cf_v2_pnl_pct": 1.0},
    ])
    _write_jsonl(tmp_path / "relax_tier_shadow.backfill.jsonl", [
        # ta_late records graded with rt_*-prefixed fields; bare outcome/pnl
        # keys stay None and must not leak into the summary.
        {"side": "long", "outcome": None, "pnl_usd": None,
         "rt_pnl_pct": 5.0, "rt_outcome": "win", "rt_graded": True},
        {"side": "short", "outcome": None, "pnl_usd": None,
         "rt_pnl_pct": -3.0, "rt_outcome": "loss", "rt_graded": True},
        {"side": "long", "outcome": None, "pnl_usd": None},
    ])
    # pullback + the two remaining artifact-less arms are deliberately absent.
    return make_backfill_client()


def test_backfill_summary_aggregates_present_files(backfill_client):
    from hermes_trader.dashboard_routes import shadow_arms
    r = backfill_client.get("/api/dashboard/shadow-arms/backfill-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["files_present"] == 4
    by_arm = {a["arm"]: a for a in body["arms"]}
    assert len(by_arm) == 7

    ta = by_arm["ta_late_entry"]
    assert ta["present"] is True and ta["records"] == 3
    assert ta["pnl"]["n"] == 3
    assert ta["pnl"]["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert ta["pnl"]["avg_pct"] == pytest.approx((4.0 - 2.0 + 3.0) / 3, abs=1e-4)
    assert ta["pnl"]["median_pct"] == 3.0
    assert ta["by_side"]["long"]["n"] == 2
    # even-n median = mean of the two middle values; also pin the min/max edges
    assert ta["by_side"]["long"]["median_pct"] == pytest.approx(1.0, abs=1e-4)
    assert ta["pnl"]["min_pct"] == -2.0 and ta["pnl"]["max_pct"] == 4.0
    assert ta["by_side"]["short"]["win_rate"] == 1.0
    assert ta["outcomes"] == {"win": 2, "loss": 1}
    assert ta["mtime"]

    xs = by_arm["xs_reversal"]
    assert xs["extras"]["candidates"] == 1
    assert xs["extras"]["forward"]["24h"]["n"] == 2
    assert xs["extras"]["forward"]["24h"]["win_rate"] == 0.0
    assert xs["extras"]["forward"]["72h"]["n"] == 1
    assert xs["extras"]["forward"]["168h"] is None

    atr = by_arm["atr_regime_calib"]
    assert atr["extras"]["would_change"] == 2
    d = atr["extras"]["calibration_delta"]
    assert d["n"] == 2
    assert d["avg_pct"] == pytest.approx((-0.6 + 0.2) / 2, abs=1e-4)
    assert d["improved"] == 1
    # change-arm: win = arm-HARMFUL, so the positive share must surface as
    # arm_harmful_rate (never a literal win_rate) with an explicit semantics tag
    assert atr["semantics"] == shadow_arms._COUNTERFACTUAL_SEMANTICS
    assert "win_rate" not in atr["pnl"]
    assert atr["pnl"]["arm_harmful_rate"] == pytest.approx(0.5, abs=1e-4)
    assert atr["pnl"]["n"] == 2  # the not_material row carries no pnl_pct
    assert atr["outcomes"] == {"win": 1, "loss": 1}

    rt = by_arm["relax_tier"]
    assert rt["present"] is True and rt["records"] == 3
    # rt_* field mapping: bare outcome/pnl_pct keys are None on this artifact
    assert rt["outcomes"] == {"win": 1, "loss": 1}
    assert rt["pnl"]["n"] == 2
    assert "win_rate" not in rt["pnl"]  # counterfactual arm semantics
    assert rt["pnl"]["arm_harmful_rate"] == pytest.approx(0.5, abs=1e-4)
    assert rt["pnl"]["avg_pct"] == pytest.approx(1.0, abs=1e-4)
    assert rt["by_side"]["long"]["n"] == 1
    assert rt["by_side"]["short"]["arm_harmful_rate"] == 0.0

    for missing in ("pullback", "daily_extension_cap", "trend_filter"):
        assert by_arm[missing]["present"] is False
        assert by_arm[missing]["records"] == 0
        assert "note" in by_arm[missing]


def test_backfill_summary_anonymous_read_and_ttl_cached(backfill_client, tmp_path):
    c = backfill_client
    r1 = c.get("/api/dashboard/shadow-arms/backfill-summary")  # no auth headers
    assert r1.status_code == 200
    assert r1.json()["files_present"] == 4
    # A new artifact landing inside the TTL window must NOT show up...
    _write_jsonl(tmp_path / "pullback_shadow.backfill.jsonl", [{"pnl_pct": -1.0}])
    r2 = c.get("/api/dashboard/shadow-arms/backfill-summary")
    assert r2.json()["files_present"] == 4
    # ...until the cache entry is invalidated/expires.
    from hermes_trader.dashboard import _TTL_CACHE
    _TTL_CACHE.pop("shadow_arms_backfill_summary", None)
    r3 = c.get("/api/dashboard/shadow-arms/backfill-summary")
    assert r3.json()["files_present"] == 5
    pb = {a["arm"]: a for a in r3.json()["arms"]}["pullback"]
    assert pb["present"] is True and pb["pnl"]["n"] == 1


def test_backfill_summary_empty_dir_is_200_not_error(make_backfill_client):
    c = make_backfill_client()  # no artifacts written at all
    r = c.get("/api/dashboard/shadow-arms/backfill-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["files_present"] == 0
    assert all(a["present"] is False for a in body["arms"])


def test_backfill_summary_tolerates_torn_trailing_line(tmp_path, make_backfill_client):
    p = tmp_path / "pullback_shadow.backfill.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"pnl_pct": 1.5, "outcome": "win"}) + "\n")
        fh.write('{"pnl_pct":')  # torn write from a killed backfiller
    c = make_backfill_client()
    r = c.get("/api/dashboard/shadow-arms/backfill-summary")
    assert r.status_code == 200
    pb = {a["arm"]: a for a in r.json()["arms"]}["pullback"]
    assert pb["records"] == 1
    assert pb["pnl"]["avg_pct"] == 1.5


# ── regen replay report (long-horizon signal-regen backtest surface) ─────────

_REGEN_REPORT = {
    "generated_at": "2026-09-15T08:36:28Z",
    "days": 120,
    "hold_bars": 2,
    "coins": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
    "window": {"t_min": 1779105600000, "t_max": 1789459200000,
               "train_end": 1785317760000, "val_end": 1787388480000},
    "n_candidates": 167580,
    "n_candidates_long72": 81886,
    "params_baseline": {"rsi_ob": 75.0, "ext_ob": 2.5},
    "plateau_picks": {"rsi_ob": None, "ext_ob": None, "adx_floor": None},
    "axis_curves": {"rsi_ob": [
        {"axis": 70.0, "ev": 0.46, "evs": {"train": -0.18, "val": 0.46, "test": -0.11},
         "n": 4679, "sign_consistent": False},
    ]},
    "overlap": {"live_rows": 35242, "matched": 35241, "block_agree_rate": 0.9999},
    "relax_tier": [{"probe": "rt_relax45"}],
    "trend_filter_sweep": [{"params": {"enabled": True}}],
    "daily_ext_cap_sweep": [{"params": {"cap": 2}}],
    "ta_late_entry_sweep": [
        {"params": {"rsi_ob": 70.0},
         "val": {"blocked": {"n": 100}, "avoided_loss_per_block": 0.5}},
        # tiny blocked cell: high avoided-loss must NOT head the trimmed table
        {"params": {"rsi_ob": 71.0},
         "val": {"blocked": {"n": 5}, "avoided_loss_per_block": 9.9}},
        {"params": {"rsi_ob": 72.0},
         "val": {"blocked": {"n": 40}, "avoided_loss_per_block": 0.3}},
    ],
}


@pytest.fixture()
def regen_client(tmp_path, monkeypatch):
    """TestClient wired to HERMES_REGEN_REPORT_FILE=tmp report, grader stubbed,
    TTL cache + regen run state reset."""
    report_path = tmp_path / "regen_report.json"
    report_path.write_text(_json.dumps(_REGEN_REPORT), encoding="utf-8")
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    monkeypatch.setenv("HERMES_REGEN_REPORT_FILE", str(report_path))
    from hermes_trader import dashboard
    from hermes_trader.dashboard_routes import shadow_arms
    monkeypatch.setattr(shadow_arms, "_load_shadow_grade", lambda: _StubGrader())
    dashboard._TTL_CACHE.clear()
    shadow_arms._REGEN_RUN_STATE.update({
        "running": False, "started_at": None, "finished_at": None,
        "exit_code": None, "days": None, "cmd": None, "error": None,
    })
    app = FastAPI()
    register_routes(app)
    return TestClient(app, raise_server_exceptions=False), report_path


def _wait_regen_done(c, timeout_s=5.0):
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        s = c.get("/api/dashboard/shadow-arms/regen-status").json()
        if not s["running"]:
            return s
        _t.sleep(0.05)
    raise AssertionError("regen run did not finish in time")


def test_regen_report_presents_trimmed_payload(regen_client):
    c, _ = regen_client
    r = c.get("/api/dashboard/shadow-arms/regen-report")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is True
    assert body["generated_at"] == "2026-09-15T08:36:28Z"  # backtest time, not fetch time
    assert body["days"] == 120 and body["hold_bars"] == 2
    assert body["n_candidates"] == 167580 and body["n_candidates_long72"] == 81886
    assert body["n_coins"] == 2 and "coins" not in body  # symbol list not copied
    assert body["mtime"] and body["cache_ttl_s"] == 60.0
    assert body["window"]["train_end"] == 1785317760000
    assert body["plateau_picks"] == {"rsi_ob": None, "ext_ob": None, "adx_floor": None}
    assert body["axis_curves"]["rsi_ob"][0]["n"] == 4679
    assert body["overlap"]["block_agree_rate"] == 0.9999
    assert body["trend_filter_sweep"] == [{"params": {"enabled": True}}]
    assert body["daily_ext_cap_sweep"] == [{"params": {"cap": 2}}]
    assert body["relax_tier"] == [{"probe": "rt_relax45"}]
    # full grid is capped to a ranked top table: tiny blocked cells rank last
    assert "ta_late_entry_sweep" not in body
    assert body["ta_late_entry_sweep_rows"] == 3
    top = body["ta_late_entry_sweep_top"]
    assert [row["params"]["rsi_ob"] for row in top] == [70.0, 72.0, 71.0]


def test_regen_report_missing_file_is_200(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_REGEN_REPORT_FILE", str(tmp_path / "nope.json"))
    from hermes_trader import dashboard
    dashboard._TTL_CACHE.clear()
    app = FastAPI()
    register_routes(app)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/dashboard/shadow-arms/regen-report")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is False and "note" in body


def test_regen_report_unreadable_json_is_200(regen_client):
    c, report_path = regen_client
    report_path.write_text('{"days":', encoding="utf-8")  # torn write
    from hermes_trader.dashboard import _TTL_CACHE
    _TTL_CACHE.pop("shadow_arms_regen_report", None)
    r = c.get("/api/dashboard/shadow-arms/regen-report")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is False and "unreadable" in body["note"]


def test_regen_report_ttl_cached_then_reflects_new_file(regen_client):
    c, report_path = regen_client
    assert c.get("/api/dashboard/shadow-arms/regen-report").json()["days"] == 120
    # A re-run lands within the TTL window: still served from cache...
    new = dict(_REGEN_REPORT, days=180, generated_at="2026-09-16T00:00:00Z")
    report_path.write_text(_json.dumps(new), encoding="utf-8")
    assert c.get("/api/dashboard/shadow-arms/regen-report").json()["days"] == 120
    # ...until the cache entry lapses/is invalidated — then the fresh replay shows.
    from hermes_trader.dashboard import _TTL_CACHE
    _TTL_CACHE.pop("shadow_arms_regen_report", None)
    body = c.get("/api/dashboard/shadow-arms/regen-report").json()
    assert body["days"] == 180 and body["generated_at"] == "2026-09-16T00:00:00Z"


def test_regen_refresh_requires_operator_token(regen_client):
    c, _ = regen_client
    r = c.post("/api/dashboard/shadow-arms/regen-refresh", json={"days": 120})
    assert r.status_code == 401


def test_regen_refresh_validates_body(regen_client):
    c, _ = regen_client
    for bad in ({"days": 10}, {"days": 500}, {"days": "abc"}, {"days": True},
                {"days": 120, "coins": "BTC; rm -rf /"}):
        r = c.post("/api/dashboard/shadow-arms/regen-refresh", json=bad, headers=_auth())
        assert r.status_code == 422, bad
    # no run was started by the rejected bodies
    assert c.get("/api/dashboard/shadow-arms/regen-status").json()["running"] is False


def test_regen_refresh_runs_and_drops_report_cache(regen_client, monkeypatch):
    c, report_path = regen_client
    from hermes_trader.dashboard_routes import shadow_arms
    from hermes_trader import session_log
    captured = []
    monkeypatch.setattr(session_log, "append", lambda ev: captured.append(ev))

    async def _fake_exec(cmd):
        new = dict(_REGEN_REPORT, days=90, generated_at="2026-09-16T00:00:00Z")
        report_path.write_text(_json.dumps(new), encoding="utf-8")
        return 0
    monkeypatch.setattr(shadow_arms, "_exec_regen", _fake_exec)

    # warm the report cache with the OLD file first
    assert c.get("/api/dashboard/shadow-arms/regen-report").json()["days"] == 120

    r = c.post("/api/dashboard/shadow-arms/regen-refresh",
               json={"days": 90}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["running"] is True and body["days"] == 90
    assert "--days 90" in body["cmd"] and "--write" in body["cmd"]
    assert str(report_path) in body["cmd"]  # --out matches the env override

    st = _wait_regen_done(c)
    assert st["exit_code"] == 0 and st["finished_at"] and st["error"] is None
    # success dropped the TTL cache: the fresh replay is visible at once
    assert c.get("/api/dashboard/shadow-arms/regen-report").json()["days"] == 90
    # audited: start + completion events (the completion event is appended from
    # the background task's finally block, just after running flips False)
    import time as _t
    deadline = _t.time() + 2.0
    while _t.time() < deadline:
        events = [e.get("event") for e in captured]
        if "shadow_arms_regen_refresh_done" in events:
            break
        _t.sleep(0.05)
    assert "shadow_arms_regen_refresh" in events
    done = next(e for e in captured if e.get("event") == "shadow_arms_regen_refresh_done")
    assert done["exit_code"] == 0 and done["via"] == "web"


def test_regen_refresh_singleflight_409(regen_client):
    # Deterministic guard: simulate an in-flight run by pre-setting running=True.
    # (A real concurrent run can't be tested here — TestClient cancels per-request
    # background tasks on response, a test-harness artifact; production uvicorn's
    # event loop persists, so fire-and-forget create_task works fine there.)
    c, _ = regen_client
    from hermes_trader.dashboard_routes import shadow_arms
    shadow_arms._REGEN_RUN_STATE["running"] = True

    r = c.post("/api/dashboard/shadow-arms/regen-refresh",
               json={"days": 120}, headers=_auth())
    assert r.status_code == 409


def test_regen_refresh_failed_run_keeps_state(regen_client, monkeypatch):
    c, _ = regen_client
    from hermes_trader.dashboard_routes import shadow_arms

    async def _boom(cmd):
        raise RuntimeError("spawn blew up")
    monkeypatch.setattr(shadow_arms, "_exec_regen", _boom)

    r = c.post("/api/dashboard/shadow-arms/regen-refresh",
               json={"days": 60}, headers=_auth())
    assert r.status_code == 200
    st = _wait_regen_done(c)
    assert st["running"] is False and "spawn blew up" in st["error"]
    # a failure must not wedge the singleflight: the next trigger is accepted
    r2 = c.post("/api/dashboard/shadow-arms/regen-refresh",
                json={"days": 60}, headers=_auth())
    assert r2.status_code == 200
    _wait_regen_done(c)


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
