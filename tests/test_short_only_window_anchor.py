"""Offline tests for the reconcile_short_only_shadow anchor fix (no network).

Audit 2026-09-15: ``fetch_hl_candles(coin, "1h", 300)`` served the LATEST 300
bars, so any signal older than the window silently mis-anchored —
``_find_entry_bar`` only checks ``t >= signal`` and the window's first bar
matched. Stubs _http_post to serve synthetic 1h OHLC bars from the request's
own startTime and verifies the explicitly-anchored window: a normal tape
walks to a short win, while a tape that does not cover the signal bar (head
gap) grades no_future_bars instead of walking the wrong tape.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_short_anchor",
    _REPO / "scripts" / "reconcile_short_only_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_short_anchor"] = mod
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
    return {"timestamp": ts or _t0_iso(), "coin": "AAA", "side": "short",
            "outcome": None,
            "detail": {"entry_px": 100.0, "confidence": 0.71,
                       "composite_score": 48, "downtrend": True}}


def test_falling_tape_sim_winner(monkeypatch):
    # Bar layout for a non-integral signal ts: idx0 = grid0-1h,
    # idx1 = grid0 (signal bar), idx2 = grid0+1h (entry bar), walk from idx3.
    # Canonical dsl defaults (max_loss 0.4 / protect 1.25 / retrace 0.2):
    # idx3 trough 97 -> idx4 floor 97.6, h=98 -> trailing stop at max(97.6, 98).
    tape = [(100, 100, 100, 100)] * 3 \
        + [(100, 100, 97.0, 98.0), (98, 98, 96.5, 97.0)] \
        + [(97, 97, 97, 97)] * 3
    monkeypatch.setattr(mod, "_http_post", _serve(tape))
    r = _rec()
    assert mod.grade_record(r, {}, {}, {}) is True
    assert r["admittable_if_enabled"] is True
    assert r["outcome"] == "sim_winner"
    assert r["exit_reason"] == "trailing_stop"
    assert r["pnl_pct"] > 0


def test_head_gap_grades_no_future_bars_not_misanchored(monkeypatch):
    # Live defect regression: with the signal bar and entry bar missing from
    # the returned tape (first bar opens at grid0+2h), the grader must refuse
    # to grade — the old code silently walked from the window's first bar.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(mod, "_http_post", _serve(tape, drop={0, 1, 2}))
    r = _rec()
    assert mod.grade_record(r, {}, {}, {}) is True
    assert r["outcome"] == "no_future_bars"
    assert "exit_px" not in r


def test_empty_response_grades_no_future_bars(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", lambda *a, **k: [])
    r = _rec()
    assert mod.grade_record(r, {}, {}, {}) is True
    assert r["outcome"] == "no_future_bars"
    assert "exit_px" not in r
