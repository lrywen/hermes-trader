"""Offline tests for reconcile_pullback_shadow (no network).

Stubs _http_post to serve synthetic 1h OHLC bars and verifies the DSL
two-phase walk: max_loss / trailing_stop / window_end geometry, the
signal_bar_open entry fallback, retryable outcomes, and — the live defect
fixed on 2026-09-15 — that a tape which does not cover the signal bar
grades no_future_bars instead of silently anchoring the walk to the
window's first bar.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_pullback",
    _REPO / "scripts" / "reconcile_pullback_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_pullback"] = mod
_spec.loader.exec_module(mod)

BAR = 3600_000
# Canonical defaults resolved by cfg_get when the live config has no
# dsl_exit block (config_store L247-251; max_loss 0.4% matches live outcomes).
CANON = {"max_loss_pct": 0.4, "protect_pct": 1.25, "retrace_threshold": 0.2}


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
         "entry_px": 0.0, "outcome": None}
    r.update(kw)
    return r


def _run(monkeypatch, tmp_path, records, write=False):
    p = tmp_path / "pb.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    argv = ["x", "--file", str(p)]
    if write:
        argv.append("--write")
    monkeypatch.setattr(sys, "argv", argv)
    # No dsl_exit block -> cfg_get falls back to the canonical DSL values.
    monkeypatch.setattr(mod, "read_agent_config", lambda: {})
    assert mod.main() == 0
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _flat_tape(monkeypatch):
    # Bar layout for a non-integral signal ts: idx0 = grid0-1h,
    # idx1 = grid0 (signal bar), idx2 = grid0+1h (entry bar), walk from idx3.
    monkeypatch.setattr(mod, "_http_post",
                        _serve([(100, 100, 100, 100)] * 8))


def test_max_loss_stop_losses(monkeypatch, tmp_path):
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 99.5, 99.6)] \
        + [(100, 100, 100, 100)] * 4
    monkeypatch.setattr(mod, "_http_post", _serve(tape))
    rows = _run(monkeypatch, tmp_path, [_rec()], write=True)
    r = rows[0]
    assert r["outcome"] == "loss"
    assert r["exit_reason"] == f"max_loss {CANON['max_loss_pct']}%"
    assert r["exit_px"] == pytest.approx(99.6)
    assert r["pnl_pct"] == pytest.approx(-0.45, abs=0.001)


def test_trailing_stop_wins(monkeypatch, tmp_path):
    tape = [(100, 100, 100, 100)] * 3 \
        + [(100, 103, 99.9, 102), (102, 102.5, 101.4, 102)] \
        + [(100, 100, 100, 100)] * 3
    monkeypatch.setattr(mod, "_http_post", _serve(tape))
    rows = _run(monkeypatch, tmp_path, [_rec()], write=True)
    r = rows[0]
    assert r["outcome"] == "win"
    assert r["exit_reason"] == "trailing_stop"
    # peak 103 after idx3; at idx4 floor = 100 + 3*(1-0.2) = 102.4,
    # exit = min(floor, bar.open) = min(102.4, 102) = 102.
    assert r["exit_px"] == pytest.approx(102.0)
    assert r["pnl_pct"] == pytest.approx(1.95, abs=0.001)


def test_window_end_flat_tape(monkeypatch, tmp_path):
    rows = _run(monkeypatch, tmp_path, [_rec()], write=True)
    r = rows[0]
    assert r["outcome"] == "loss"  # fees only
    assert r["exit_reason"] == "window_end"
    assert r["pnl_pct"] == pytest.approx(-0.05, abs=0.001)


def test_head_gap_grades_no_future_bars_not_misanchored(monkeypatch, tmp_path):
    # Live defect regression: with the signal bar and entry bar missing from
    # the returned tape (first bar opens at grid0+2h), the grader must refuse
    # to grade — the old code silently walked from the window's first bar.
    tape = [(100, 100, 100, 100)] * 3 + [(100, 100, 90.0, 95.0)] \
        + [(95, 95, 95, 95)] * 4
    monkeypatch.setattr(mod, "_http_post", _serve(tape, drop={0, 1, 2}))
    rows = _run(monkeypatch, tmp_path, [_rec()], write=True)
    assert rows[0]["outcome"] == "no_future_bars"
    assert "exit_px" not in rows[0]


def test_entry_fallback_marks_signal_bar_open(monkeypatch, tmp_path):
    tape = [(100, 100, 100, 100)] * 2 + [(123, 123, 123, 123)] \
        + [(123, 123, 123, 123)] * 5
    monkeypatch.setattr(mod, "_http_post", _serve(tape))
    rows = _run(monkeypatch, tmp_path, [_rec(entry_px=0.0)], write=True)
    r = rows[0]
    assert r["entry_px_source"] == "signal_bar_open"
    # entry was the grid0+1h bar open (123), flat afterwards -> fees only
    assert r["pnl_pct"] == pytest.approx(-0.05, abs=0.001)


def test_explicit_entry_px_used_without_source_tag(monkeypatch, tmp_path):
    rows = _run(monkeypatch, tmp_path, [_rec(entry_px=80.0)], write=True)
    r = rows[0]
    assert "entry_px_source" not in r
    # flat 100 tape, entry 80 -> window_end at 100 -> +25% - fees
    assert r["pnl_pct"] == pytest.approx(25.0 - 0.05, abs=0.001)


def test_retryable_and_terminal_outcomes(monkeypatch, tmp_path):
    retry = _rec(outcome="no_entry_px")
    term = _rec(outcome="win", pnl_pct=9.9)
    rows = _run(monkeypatch, tmp_path, [retry, term], write=True)
    assert rows[0]["outcome"] == "loss"  # re-graded (flat tape -> fees)
    assert rows[0]["exit_reason"] == "window_end"
    assert rows[1]["outcome"] == "win"   # untouched
    assert rows[1]["pnl_pct"] == 9.9


def test_dry_run_does_not_write(monkeypatch, tmp_path):
    rows = _run(monkeypatch, tmp_path, [_rec()], write=False)
    assert rows[0]["outcome"] is None
