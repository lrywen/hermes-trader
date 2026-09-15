"""Offline tests for the signal-anchored window fetch in
reconcile_ta_late_entry_shadow and reconcile_change_arms_shadow (no network).

Stubs _http_post to serve synthetic OHLC bars keyed off the request's
startTime and verifies — the live defect fixed on 2026-09-15 — that a tape
which does not cover the signal bar grades no_future_bars instead of
silently anchoring the walk to the window's first bar, plus the (coin,
grid0) cache-key split in reconcile_change_arms_shadow.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
# reconcile_change_arms_shadow imports shadow_progress from scripts/.
sys.path.insert(0, str(_REPO / "scripts"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


ta_mod = _load("reconcile_ta_late", "scripts/reconcile_ta_late_entry_shadow.py")
ca_mod = _load("reconcile_change_arms", "scripts/reconcile_change_arms_shadow.py")

BAR_4H = 14_400_000
BAR_1H = 3600_000
FEE_PCT = 0.0005


def _t0_iso(age_h=100):
    dt = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return dt.isoformat().replace("+00:00", "Z")


def _t0_ms(age_h=100):
    return (datetime.now(timezone.utc) - timedelta(hours=age_h)).timestamp() * 1000.0


def _serve(ohlc, drop=(), bar_ms=BAR_1H, calls=None):
    """Fake _http_post: bars open on exact bar_ms boundaries from the
    request's startTime (like the real API). Indices in `drop` are omitted
    to simulate head/mid gaps; remaining bars keep absolute open times."""
    def _post(path, payload, *a, **k):
        if calls is not None:
            calls.append(payload)
        req = payload.get("req", {})
        start = int(req["startTime"])
        first = -(-start // bar_ms) * bar_ms
        out = []
        for i, (o, h, l, c) in enumerate(ohlc):
            if i in drop:
                continue
            out.append({"t": first + i * bar_ms, "o": str(o), "h": str(h),
                        "l": str(l), "c": str(c), "v": "1"})
        return out
    return _post


# --- ta_late_entry (4h geometry, driven through main()) --------------------

def _ta_rec(ts=None, **kw):
    r = {"timestamp": ts or _t0_iso(), "coin": "AAA", "side": "long",
         "blocked": True, "layer": "gate", "entry_px": 100.0,
         "trade_notional_usd": 200, "outcome": None}
    r.update(kw)
    return r


def _ta_run(monkeypatch, tmp_path, records, write=True):
    p = tmp_path / "ta.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    argv = ["x", "--file", str(p)]
    if write:
        argv.append("--write")
    monkeypatch.setattr(sys, "argv", argv)
    assert ta_mod.main() == 0
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_ta_late_normal_tape_grades(monkeypatch, tmp_path):
    tape = [(100, 100, 100, 100)] * 8
    monkeypatch.setattr(ta_mod, "_http_post", _serve(tape, bar_ms=BAR_4H))
    rows = _ta_run(monkeypatch, tmp_path, [_ta_rec()])
    r = rows[0]
    assert r["outcome"] in ("win", "loss")
    assert "exit_px" in r
    assert "mae_pct" in r


def test_ta_late_head_gap_not_misanchored(monkeypatch, tmp_path):
    # Live defect regression: grid0-8h / grid0-4h / grid0 missing from the
    # returned tape. The grader must refuse to grade — the old latest-N-bars
    # code silently walked from the window's first bar.
    tape = [(100, 100, 100, 100)] * 8
    monkeypatch.setattr(ta_mod, "_http_post",
                        _serve(tape, drop={0, 1, 2}, bar_ms=BAR_4H))
    rows = _ta_run(monkeypatch, tmp_path, [_ta_rec()])
    assert rows[0]["outcome"] == "no_future_bars"
    assert "exit_px" not in rows[0]


# --- change_arms (1h geometry, reconcile_arm() driven directly) -------------

def _ca_rec(ts_ms=None, **kw):
    r = {"ts": ts_ms if ts_ms is not None else _t0_ms(), "coin": "AAA",
         "side": "long", "would_block_gate": True, "verdict": "LONG",
         "outcome": None}
    r.update(kw)
    return r


def test_change_arms_normal_tape_settles(monkeypatch):
    tape = [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(30)]
    monkeypatch.setattr(ca_mod, "_http_post", _serve(tape, bar_ms=BAR_1H))
    recs = [_ca_rec()]
    ca_mod.reconcile_arm("confidence_decay", recs, FEE_PCT, 24, 30)
    r = recs[0]
    assert r["outcome"] in ("win", "loss")
    # Entry is the open of the first 1h bar strictly after the signal:
    # tape idx0 = grid0-1h, idx1 = grid0, idx2 = grid0+1h -> open 102.
    assert r["cf_entry_px"] == pytest.approx(102.0)
    assert r["cf_entry"] == "bar_open"


def test_change_arms_head_gap_not_misanchored(monkeypatch):
    # Live defect regression: first served bar opens at grid0+2h, beyond one
    # bar step of the signal bar -> refuse to settle on the wrong tape.
    tape = [(100, 100, 100, 100)] * 30
    monkeypatch.setattr(ca_mod, "_http_post",
                        _serve(tape, drop={0, 1, 2}, bar_ms=BAR_1H))
    recs = [_ca_rec()]
    ca_mod.reconcile_arm("confidence_decay", recs, FEE_PCT, 24, 30)
    assert recs[0]["outcome"] == "no_future_bars"
    assert "cf_entry_px" not in recs[0]


def test_change_arms_cache_keyed_by_coin_and_signal_bar(monkeypatch):
    # Two records for the same coin 5h apart must NOT share one tape: the
    # window is signal-anchored, so each (coin, grid0) fetches its own.
    tape = [(100, 100, 100, 100)] * 30
    calls = []
    monkeypatch.setattr(ca_mod, "_http_post",
                        _serve(tape, bar_ms=BAR_1H, calls=calls))
    ts1 = _t0_ms(100)
    ts2 = _t0_ms(105)
    recs = [_ca_rec(ts1), _ca_rec(ts2)]
    ca_mod.reconcile_arm("confidence_decay", recs, FEE_PCT, 24, 30)
    assert len(calls) == 2
    starts = {int(c["req"]["startTime"]) for c in calls}
    assert len(starts) == 2
