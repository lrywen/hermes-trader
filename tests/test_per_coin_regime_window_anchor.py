"""Offline tests for reconcile_per_coin_regime_shadow window anchoring.

Stubs _http_post to serve synthetic 1h OHLC bars and verifies the explicit
time-anchored fetch window: a normal tape grades sim_winner, and — the live
defect fixed on 2026-09-15 — a tape which does not cover the signal bar
(head gap) grades no_future_bars instead of silently anchoring the DSL walk
to the window's first bar.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_per_coin_anchor",
    _REPO / "scripts" / "reconcile_per_coin_regime_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_per_coin_anchor"] = mod
_spec.loader.exec_module(mod)

BAR = 3600_000


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


def _rec(ts=None, **kw):
    r = {"timestamp": ts or _t0_iso(), "coin": "AAA", "side": "long",
         "outcome": None, "would": "pass",
         "detail": {"quadrant_tier": "mid", "macro_regime": "up",
                    "own_1h_regime": "up", "own_1h_score": 0.5}}
    r.update(kw)
    return r


def test_rising_tape_grades_sim_winner(monkeypatch):
    # Entry at the grid0+1h bar open (100); tape climbs to 106 and the walk
    # ends at window_end well above entry -> sim_winner after fees.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 105, 100, 105)] \
        + [(105, 106, 105, 106)] * 4
    monkeypatch.setattr(mod, "_http_post", _serve(tape))
    r = _rec()
    assert mod.grade_record(r, {}, {}) is True
    assert r["outcome"] == "sim_winner"
    assert r["pnl_pct"] > 0
    assert r["entry_px"] == pytest.approx(100.0)


def test_head_gap_grades_no_future_bars_not_misanchored(monkeypatch):
    # Live defect regression: with the lead bar, signal bar and entry bar
    # missing from the returned tape (first bar opens at grid0+2h), the
    # grader must refuse to grade — the old code silently walked from the
    # window's first bar (here a falling tape that would misgrade a loser).
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(mod, "_http_post", _serve(tape, drop={0, 1, 2}))
    r = _rec()
    assert mod.grade_record(r, {}, {}) is True
    assert r["outcome"] == "no_future_bars"
    assert "entry_px" not in r
    assert "exit_px" not in r


def test_empty_response_grades_no_future_bars(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", lambda *a, **k: [])
    r = _rec()
    assert mod.grade_record(r, {}, {}) is True
    assert r["outcome"] == "no_future_bars"
    assert "entry_px" not in r
    assert "exit_px" not in r
