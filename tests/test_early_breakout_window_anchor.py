"""Offline tests for the time-anchored window fetch in
reconcile_early_breakout_shadow (Audit 2026-09-15).

Regression for the silent mis-anchoring defect: the old
``fetch_hl_candles(coin, "1h", 300)`` call returned the LATEST 300 bars, so
any signal older than that window matched the window's first bar and the
whole two-phase walk ran on the wrong tape with no error. The grader now
requests an explicit [grid0-1h, grid0+(SIM_BARS+4)h] window and refuses to
grade when the returned tape does not cover the signal bar (head gap).
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_early_anchor",
    _REPO / "scripts" / "reconcile_early_breakout_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_early_anchor"] = mod
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


def _rec(ts=None):
    return {"timestamp": ts or _t0_iso(), "coin": "AAA", "side": "long",
            "outcome": None,
            "detail": {"entry_px": 100.0, "atr4h_pct": 1.0,
                       "early_stop_atr_mult": 1.2,
                       "early_size_fraction": 0.5}}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # No live config -> cfg_get falls back to canonical/script defaults.
    monkeypatch.setattr(mod, "read_agent_config", lambda: {})


def test_normal_tape_grades_with_exit_px(monkeypatch):
    # Flat tape at the entry price: no stop, no trailing -> window_end,
    # fees-only sim_loser. Proves the anchored fetch did not break grading.
    monkeypatch.setattr(mod, "_http_post",
                        _serve([(100, 100, 100, 100)] * 8))
    r = _rec()
    assert mod.grade_record(r, {}, {}) is True
    assert r["outcome"] in ("sim_winner", "sim_loser")
    assert "exit_px" in r


def test_head_gap_grades_no_future_bars_not_misanchored(monkeypatch):
    # Core regression: grid0-1h, grid0 and grid0+1h are all missing, so the
    # first returned bar opens at grid0+2h. The old code silently walked
    # from that first bar; the guard must refuse to grade.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(mod, "_http_post", _serve(tape, drop={0, 1, 2}))
    r = _rec()
    assert mod.grade_record(r, {}, {}) is True
    assert r["outcome"] == "no_future_bars"
    assert "exit_px" not in r


def test_empty_response_grades_no_future_bars(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", lambda *a, **k: [])
    r = _rec()
    assert mod.grade_record(r, {}, {}) is True
    assert r["outcome"] == "no_future_bars"
    assert "exit_px" not in r
