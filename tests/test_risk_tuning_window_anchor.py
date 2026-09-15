"""Offline tests for the time-anchored candle window fix (2026-09-15).

Covers scripts/reconcile_risk_tuning_shadow.py and
scripts/pullback_shadow_daily.py: both replaced ``fetch_hl_candles(coin,
"1h", 300)`` with an explicit [grid0-1h, grid0+(WALK+4)h] window anchored
at the signal timestamp, plus an anchoring guard. Regression target: when
the returned tape does not cover the signal bar (head gap), the graders
must refuse to walk — the old code silently anchored the whole DSL walk
to the window's first bar.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, _REPO / "scripts" / filename)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


rt = _load("reconcile_risk_tuning_anchor", "reconcile_risk_tuning_shadow.py")
pbd = _load("pullback_shadow_daily_anchor", "pullback_shadow_daily.py")

BAR = 3600_000
_DSL = {"max_loss_pct": 0.8, "protect_pct": 2.5, "retrace_threshold": 0.5}


def _t0_iso(age_h=100):
    dt = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return dt.isoformat().replace("+00:00", "Z")


def _serve(ohlc, drop=()):
    """Fake _http_post: bars open on exact hour boundaries from the
    request's startTime (like the real API). Indices in `drop` are omitted
    to simulate head/mid gaps; remaining bars keep absolute open times."""
    def _post(path, payload, *a, **k):
        req = payload.get("req", {})
        start = int(req["startTime"])
        first = -(-start // BAR) * BAR
        out = []
        for i, (o, h, l, c) in enumerate(ohlc):
            if i in drop:
                continue
            out.append({"t": first + i * BAR, "o": str(o), "h": str(h),
                        "l": str(l), "c": str(c), "v": "1"})
        return out
    return _post


# --- reconcile_risk_tuning_shadow.grade_record -----------------------------

def test_risk_tuning_block_proposal_normal_tape(monkeypatch):
    # idx0=grid0-1h, idx1=grid0 (signal bar), idx2=entry bar; rally to +3%
    # then flat -> window_end at 103 -> counter-factual winner.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 103, 100, 103)] \
        + [(103, 103, 103, 103)] * 6
    monkeypatch.setattr(rt, "_http_post", _serve(tape))
    r = {"timestamp": _t0_iso(), "rule": "breakout_score_floor",
         "coin": "AAA", "side": "long",
         "detail": {"entry_px": 100.0}, "outcome": None}
    ok = rt.grade_record(r, dict(_DSL), closes_by_coin={})
    assert ok is True
    assert r["outcome"] in ("blocked_a_winner",)
    assert r["pnl_pct"] > 0


def test_risk_tuning_head_gap_leaves_pending(monkeypatch):
    # Live defect regression (generic path): with the signal bar and entry
    # bar missing (first bar opens at grid0+2h), the grader must stay
    # pending — the old code silently walked from the window's first bar.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(rt, "_http_post", _serve(tape, drop={0, 1, 2}))
    r = {"timestamp": _t0_iso(), "rule": "breakout_score_floor",
         "coin": "AAA", "side": "long",
         "detail": {"entry_px": 100.0}, "outcome": None}
    ok = rt.grade_record(r, dict(_DSL), closes_by_coin={})
    assert ok is False
    assert r["outcome"] is None
    assert "exit_px" not in r


def test_risk_tuning_stop_tuning_head_gap_skips_sim_fields(monkeypatch):
    # stop_tuning semantics: on a head gap the outcome is still written
    # (fallback wider_cap_no_gain) but no wider_cap_sim / live_cap_sim.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(rt, "_http_post", _serve(tape, drop={0, 1, 2}))
    r = {"timestamp": _t0_iso(), "rule": "stop_tuning", "coin": "AAA",
         "side": "long", "would": "survive_wider_cap",
         "detail": {"entry_px": 100.0, "live_spot_cap_pct": 0.8,
                    "candidate_max_loss_pct": 1.5}, "outcome": None}
    ok = rt.grade_record(r, dict(_DSL), closes_by_coin={})
    assert ok is True
    assert r["outcome"] == "wider_cap_no_gain"
    assert "wider_cap_sim" not in r
    assert "live_cap_sim" not in r


# --- pullback_shadow_daily.reconcile ---------------------------------------

def test_pullback_daily_normal_tape_grades_loss(monkeypatch):
    # Entry bar at idx2; idx3 dips through the canonical 0.4% max_loss.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 98.0, 98.5)] \
        + [(98.5, 98.5, 98.5, 98.5)] * 5
    monkeypatch.setattr(pbd, "_http_post", _serve(tape))
    monkeypatch.setattr(pbd, "read_agent_config", lambda: {})
    r = {"timestamp": _t0_iso(), "coin": "AAA", "side": "long",
         "entry_px": 100.0, "outcome": None}
    pbd.reconcile([r], 24, force=False)
    assert r["outcome"] == "loss"
    assert r["exit_reason"]


def test_pullback_daily_head_gap_no_future_bars(monkeypatch):
    # Head-gap regression: first returned bar opens at grid0+2h -> the
    # reconciler must grade no_future_bars, never walk the wrong tape.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(pbd, "_http_post", _serve(tape, drop={0, 1, 2}))
    monkeypatch.setattr(pbd, "read_agent_config", lambda: {})
    r = {"timestamp": _t0_iso(), "coin": "AAA", "side": "long",
         "entry_px": 100.0, "outcome": None}
    pbd.reconcile([r], 24, force=False)
    assert r["outcome"] == "no_future_bars"
    assert "exit_px" not in r
