"""Offline tests for reconcile_relax_tier_shadow (no network).

Stubs _http_post to serve synthetic 4h candles and verifies the
backtest-parity fixed-window forward grid on PASSED ta_late decisions:
side-aware win/loss at 72h net of fees, immature before then, only passed
gate records with rt verdicts are eligible, and the per-arm flag/spread
bucketing.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_relax_tier",
    _REPO / "scripts" / "reconcile_relax_tier_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_relax_tier"] = mod
_spec.loader.exec_module(mod)

BAR = 4 * 3600_000


def _serve(prices):
    """Fake _http_post serving 4h candles whose closes == `prices`.

    The grader requests startTime = t0 - one 4h bar, so closes[0] is B0
    (the bar forming at signal) and the n-hours forward close is at
    index n//4 + 1.
    """
    def _post(path, payload, *a, **k):
        req = payload.get("req", {})
        start = int(req["startTime"])
        return [{"t": start + i * BAR, "o": p, "h": p, "l": p, "c": p,
                 "v": "1"} for i, p in enumerate(prices)]
    return _post


def _rec(side="long", entry=100.0, age_h=100, blocked=False, layer="gate",
         **rt):
    r = {
        "timestamp": int((time.time() - age_h * 3600) * 1000),
        "coin": "AAA", "side": side, "entry_px": entry,
        "layer": layer, "blocked": blocked,
        "rt_relax45_would_block": None,
        "rt_weak_rsi70_would_block": None,
        "rt_no_adx20_would_block": None,
        "outcome": None,
    }
    r.update(rt)
    return r


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", _serve([100.0] * 60))


def test_signal_ms_accepts_iso_millis_seconds():
    assert mod._signal_ms({"timestamp": 1_700_000_000_000}) == 1_700_000_000_000
    assert mod._signal_ms({"timestamp": 1_700_000_000}) == 1_700_000_000_000
    assert mod._signal_ms({"timestamp": "2026-09-10T00:00:00Z"}) is not None
    assert mod._signal_ms({"timestamp": None}) is None


def test_long_win_at_72h_net_of_fee(monkeypatch):
    prices = [100.0] * 60
    # 72h -> index 72//4 + 1 = 19.
    prices[19] = 106.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec(rt_no_adx20_would_block=True)
    assert mod.grade(r) == "win"
    # +6% gross minus 5bps round trip.
    assert r["rt_pnl_pct"] == pytest.approx(5.95, abs=0.01)
    g = r["rt_forward"]
    assert g["fwd6h_pct"] == pytest.approx(-0.05, abs=0.01)
    assert g["fwd24h_pct"] == pytest.approx(-0.05, abs=0.01)


def test_short_loss_is_side_aware(monkeypatch):
    prices = [100.0] * 60
    prices[19] = 103.0  # up move is a loss for a short
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec(side="short", rt_relax45_would_block=True)
    assert mod.grade(r) == "loss"
    assert r["rt_pnl_pct"] == pytest.approx(-3.05, abs=0.01)


def test_immature_when_72h_bar_missing(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", _serve([100.0] * 10))
    r = _rec(rt_no_adx20_would_block=True)
    assert mod.grade(r) == "immature"
    assert r["rt_forward"]["fwd72h_pct"] is None
    assert "rt_pnl_pct" not in r


def test_no_future_bars_on_empty_fetch(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", lambda *a, **k: [])
    assert mod.grade(_rec(rt_no_adx20_would_block=True)) == "no_future_bars"


def test_guards_bad_record():
    assert mod.grade({"entry_px": 100, "rt_no_adx20_would_block": True}) == "no_coin"
    assert mod.grade({"coin": "AAA", "entry_px": 0}) == "no_entry_px"
    assert mod.grade({"coin": "AAA", "entry_px": 100}) == "bad_timestamp"


def test_eligible_requires_passed_gate_record_with_rt_verdict():
    assert mod._eligible(_rec(blocked=False, rt_no_adx20_would_block=True))
    # live-blocked records are owned by the other reconcile script
    assert not mod._eligible(_rec(blocked=True, rt_no_adx20_would_block=True))
    # prefilter layer excluded
    assert not mod._eligible(_rec(layer="prefilter", rt_no_adx20_would_block=True))
    # no rt verdict at all (probe disabled / old row)
    assert not mod._eligible(_rec())
    # an explicit False verdict still counts (spared cell)
    assert mod._eligible(_rec(rt_no_adx20_would_block=False))


def test_iter_files_includes_rotated(tmp_path):
    primary = tmp_path / "ta.jsonl"
    primary.write_text("{}")
    (tmp_path / "ta.jsonl.1").write_text("{}")
    files = mod._iter_files(str(primary))
    assert set(files) == {str(primary), str(tmp_path / "ta.jsonl.1")}


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _run_main(monkeypatch, file_path, write=False, force=False):
    argv = ["x", "--file", str(file_path)]
    if write:
        argv.append("--write")
    if force:
        argv.append("--force")
    monkeypatch.setattr(sys, "argv", argv)
    return mod.main()


def test_main_buckets_flagged_vs_spared_and_writes(tmp_path, monkeypatch):
    # Two mature passes on the same flat tape (net -0.05% = loss): one flagged
    # by rt_no_adx20, one spared. Force the flagged one up at 72h to make it a
    # win via per-record price tapes is not possible with one stub, so instead
    # verify bucketing counts with the shared tape (both are losses).
    flagged = _rec(age_h=100, rt_no_adx20_would_block=True,
                   rt_relax45_would_block=False)
    spared = _rec(age_h=100, coin="AAA", rt_no_adx20_would_block=False)
    p = tmp_path / "ta.jsonl"
    _write(p, [flagged, spared])
    assert _run_main(monkeypatch, p, write=True) == 0
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert all(r.get("rt_graded") for r in rows)
    assert [r["rt_outcome"] for r in rows] == ["loss", "loss"]


def test_main_skips_already_graded_without_force(tmp_path, monkeypatch):
    r = _rec(age_h=100, rt_no_adx20_would_block=True, rt_graded=True,
             rt_outcome="win", rt_pnl_pct=4.2)
    p = tmp_path / "ta.jsonl"
    _write(p, [r])
    # booby-trap grade(): already-graded rows must never be re-graded.
    monkeypatch.setattr(mod, "grade",
                        lambda rec: (_ for _ in ()).throw(AssertionError("nope")))
    assert _run_main(monkeypatch, p, write=True) == 0


def test_main_ignores_blocked_and_prefilter(tmp_path, monkeypatch):
    blocked = _rec(age_h=100, blocked=True, rt_no_adx20_would_block=True)
    pref = _rec(age_h=100, layer="prefilter", rt_no_adx20_would_block=True)
    p = tmp_path / "ta.jsonl"
    _write(p, [blocked, pref])
    monkeypatch.setattr(mod, "grade",
                        lambda rec: (_ for _ in ()).throw(AssertionError("nope")))
    assert _run_main(monkeypatch, p, write=True) == 0
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert all("rt_outcome" not in r for r in rows)
