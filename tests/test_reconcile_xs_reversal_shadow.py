"""Offline tests for reconcile_xs_reversal_shadow (no network).

Stubs _http_post to serve synthetic 1h candles and verifies the M1-parity
fixed-window forward grid: win/loss at 72h, immature before then,
no-entry/no-future guards, and millisecond timestamp parsing.
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
    "reconcile_xs",
    _REPO / "scripts" / "reconcile_xs_reversal_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_xs"] = mod
_spec.loader.exec_module(mod)


def _serve(prices, drop=()):
    """Return a fake _http_post serving candles whose closes == `prices`.

    Bars open on exact hour boundaries (like the real API): the first bar is
    the first hour boundary >= startTime. Indices in `drop` are omitted to
    simulate holes/head offsets; the remaining bars keep their absolute open
    times, so a well-anchored grader must not shift.
    """
    def _post(path, payload, *a, **k):
        req = payload.get("req", {})
        start = int(req["startTime"])
        first = -(-start // 3600_000) * 3600_000
        return [{"t": first + i * 3600_000, "o": p, "h": p, "l": p, "c": p,
                 "v": "1"} for i, p in enumerate(prices) if i not in drop]
    return _post


def _rec(entry=100.0, age_h=100, candidate=False, macro="NEUTRAL"):
    return {
        "timestamp": int((time.time() - age_h * 3600) * 1000),
        "coin": "AAA", "side": "long", "entry_px": entry,
        "is_candidate": candidate, "macro_regime": macro,
        "outcome": None,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Default: a long, flat then up tape; individual tests override.
    monkeypatch.setattr(mod, "_http_post", _serve([100.0] * 200))


def test_signal_ms_accepts_millis_and_iso():
    assert mod._signal_ms({"timestamp": 1_700_000_000_000}) == 1_700_000_000_000
    # seconds epoch gets scaled
    assert mod._signal_ms({"timestamp": 1_700_000_000}) == 1_700_000_000_000
    assert mod._signal_ms({"timestamp": "2026-09-10T00:00:00Z"}) is not None
    assert mod._signal_ms({"timestamp": None}) is None


def test_win_when_72h_up(monkeypatch):
    prices = [100.0] * 200
    # index 72 = the close of the bar opening at grid0 + 72h
    prices[72] = 105.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec()
    assert mod.grade(r) == "win"
    assert r["pnl_pct"] == pytest.approx(5.0, abs=0.01)
    assert r["forward"]["fwd24h_pct"] == pytest.approx(0.0, abs=0.01)
    assert r["forward"]["fwd168h_pct"] == pytest.approx(0.0, abs=0.01)


def test_loss_when_72h_down(monkeypatch):
    prices = [100.0] * 200
    prices[72] = 94.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec()
    assert mod.grade(r) == "loss"
    assert r["pnl_pct"] == pytest.approx(-6.0, abs=0.01)


def test_head_offset_does_not_shift_grid(monkeypatch):
    # A missing head bar must not shift the forward grid (the live defect
    # this fix addresses): same absolute tape, with and without bar 0.
    prices = [100.0] * 200
    prices[24] = 102.0
    prices[72] = 105.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec()
    assert mod.grade(r) == "win"
    base = dict(r["forward"])
    monkeypatch.setattr(mod, "_http_post", _serve(prices, drop={0}))
    r2 = _rec()
    assert mod.grade(r2) == "win"
    assert r2["forward"] == base


def test_mid_window_gap_does_not_shift_grid(monkeypatch):
    prices = [100.0] * 200
    prices[72] = 105.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices, drop={10, 50}))
    r = _rec()
    assert mod.grade(r) == "win"
    assert r["forward"]["fwd72h_px"] == pytest.approx(105.0)


def test_missing_primary_bar_is_immature_not_shifted(monkeypatch):
    # A hole exactly at the 72h bar must grade immature — never borrow a
    # neighbouring bar's close.
    prices = [100.0] * 200
    prices[24] = 102.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices, drop={72}))
    r = _rec()
    assert mod.grade(r) == "immature"
    assert r["forward"]["fwd72h_pct"] is None
    assert r["forward"]["fwd24h_pct"] == pytest.approx(2.0, abs=0.01)


def test_mature_vocabulary_matches_grader():
    # The grader (shadow_grade._window_stats) only counts outcomes in
    # ("win", "loss"). Pin the contract so a future vocabulary drift cannot
    # silently make xs_reversal outcomes invisible again.
    assert mod.MATURE_OUTCOMES == ("win", "loss")
    assert mod._LEGACY_OUTCOME == {"winner": "win", "loser": "loss"}


def test_immature_when_72h_bar_missing(monkeypatch):
    # only 30 bars → 72h close not available yet
    monkeypatch.setattr(mod, "_http_post", _serve([100.0] * 30))
    r = _rec(age_h=100)
    assert mod.grade(r) == "immature"
    assert r["forward"]["fwd24h_pct"] == pytest.approx(0.0, abs=0.01)
    assert r["forward"]["fwd72h_pct"] is None


def test_no_future_bars_on_empty_fetch(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", lambda *a, **k: [])
    assert mod.grade(_rec()) == "no_future_bars"


def test_guards_bad_record():
    assert mod.grade({"coin": "AAA", "entry_px": 0}) == "no_entry_px"
    assert mod.grade({"coin": "AAA", "entry_px": 100}) == "bad_timestamp"
    assert mod.grade({"entry_px": 100, "timestamp": int(time.time()*1000)}) == "no_coin"


def test_iter_files_includes_rotated(tmp_path):
    primary = tmp_path / "xs.jsonl"
    primary.write_text("{}")
    (tmp_path / "xs.jsonl.1").write_text("{}")
    (tmp_path / "xs.jsonl.2").write_text("{}")
    files = mod._iter_files(str(primary))
    assert set(files) == {str(primary), str(tmp_path / "xs.jsonl.1"),
                          str(tmp_path / "xs.jsonl.2")}


def _run_main(monkeypatch, file_path, write=False, boom_grade=False):
    argv = ["x", "--file", str(file_path)]
    if write:
        argv.append("--write")
    monkeypatch.setattr(sys, "argv", argv)
    if boom_grade:
        # Legacy rows must be treated as already-graded and never re-graded.
        monkeypatch.setattr(mod, "grade", lambda r: (_ for _ in ()).throw(
            AssertionError("grade() must not run on legacy mature outcomes")))
    return mod.main()


def _legacy_file(tmp_path):
    p = tmp_path / "xs.jsonl"
    p.write_text("\n".join([
        json.dumps({"timestamp": int((time.time() - 100 * 3600) * 1000),
                    "coin": "AAA", "side": "long", "entry_px": 100.0,
                    "outcome": "winner", "pnl_pct": 5.0}),
        json.dumps({"timestamp": int((time.time() - 100 * 3600) * 1000),
                    "coin": "BBB", "side": "long", "entry_px": 200.0,
                    "outcome": "loser", "pnl_pct": -3.0}),
    ]) + "\n")
    return p


def test_legacy_outcomes_skip_regrade_and_normalize_on_write(tmp_path, monkeypatch):
    import json
    p = _legacy_file(tmp_path)
    # grade() is booby-trapped: legacy winner/loser must not be sent to grade().
    assert _run_main(monkeypatch, p, write=True, boom_grade=True) == 0
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert [r["outcome"] for r in rows] == ["win", "loss"]


def test_legacy_outcomes_not_rewritten_in_dry_run(tmp_path, monkeypatch):
    import json
    p = _legacy_file(tmp_path)
    assert _run_main(monkeypatch, p, write=False, boom_grade=True) == 0
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert [r["outcome"] for r in rows] == ["winner", "loser"]
