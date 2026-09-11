"""Offline tests for reconcile_daily_extension_cap_shadow (no network).

Stubs _http_post to serve synthetic 1h candles and verifies the
counterfactual geometry: the would-be chase entry is the signal-bar close
(index 1), win/loss at the 72h forward close, immature before then, long-only,
data_missing rows skipped, and ISO timestamp parsing.
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
    "reconcile_daily_ext",
    _REPO / "scripts" / "reconcile_daily_extension_cap_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_daily_ext"] = mod
_spec.loader.exec_module(mod)


def _serve(prices):
    """Fake _http_post: closes[i] is signal-relative bar (i-1); index 0 pre-bar.

    The grader requests startTime = t0-1h, so row 0 is the pre-bar and row 1 is
    the signal bar (the would-be chase entry).
    """
    def _post(path, payload, *a, **k):
        req = payload.get("req", {})
        start = int(req["startTime"])
        return [{"t": start + i * 3600_000, "o": p, "h": p, "l": p, "c": p,
                 "v": "1"} for i, p in enumerate(prices)]
    return _post


def _iso_hours_ago(hours):
    return _iso_ms(int((time.time() - hours * 3600) * 1000))


def _iso_ms(ms):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _rec(age_h=100, side="long", state="ok", change=35.0, would=True):
    return {
        "timestamp": _iso_hours_ago(age_h),
        "coin": "AAA", "side": side, "mode": "shadow",
        "cap_pct": 30.0, "state": state,
        "daily_change_pct": change, "ext_would_block": would,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", _serve([100.0] * 200))


def test_signal_ms_parses_iso_and_epoch():
    s = _rec()["timestamp"]
    ms = mod._signal_ms({"timestamp": s})
    assert isinstance(ms, int) and ms > 1_000_000_000_000
    assert mod._signal_ms({"timestamp": 1_700_000_000_000}) == 1_700_000_000_000
    assert mod._signal_ms({"timestamp": 1_700_000_000}) == 1_700_000_000_000
    assert mod._signal_ms({"timestamp": "not-a-date"}) is None
    assert mod._signal_ms({"timestamp": None}) is None


def test_win_when_72h_forward_up(monkeypatch):
    prices = [100.0] * 200
    # signal-bar entry = index 1 = 100; 72h forward close = index 73
    prices[73] = 106.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec()
    assert mod.grade(r) == "win"
    assert r["entry_px"] == pytest.approx(100.0)
    assert r["pnl_pct"] == pytest.approx(6.0, abs=0.01)


def test_loss_when_72h_forward_down(monkeypatch):
    prices = [100.0] * 200
    prices[73] = 92.0
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec()
    assert mod.grade(r) == "loss"
    assert r["pnl_pct"] == pytest.approx(-8.0, abs=0.01)


def test_extended_chase_entry_uses_signal_bar_close(monkeypatch):
    # pre-bar (idx0) != signal bar (idx1): entry must be the signal-bar close,
    # i.e. the price at the moment the gate fired (the chase), not the prior bar.
    prices = [100.0] + [130.0] + [130.0] * 198
    prices[73] = 143.0   # +10% off the 130 chase entry
    monkeypatch.setattr(mod, "_http_post", _serve(prices))
    r = _rec(change=45.0)
    assert mod.grade(r) == "win"
    assert r["entry_px"] == pytest.approx(130.0)
    assert r["pnl_pct"] == pytest.approx(10.0, abs=0.01)


def test_immature_when_72h_bar_missing(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", _serve([100.0] * 30))
    r = _rec()
    assert mod.grade(r) == "immature"
    assert r["forward"]["fwd72h_pct"] is None


def test_no_future_bars_on_empty_fetch(monkeypatch):
    monkeypatch.setattr(mod, "_http_post", lambda *a, **k: [])
    assert mod.grade(_rec()) == "no_future_bars"


def test_data_missing_row_not_graded():
    assert mod.grade(_rec(state="data_missing", change=None, would=None)) == "data_missing"
    assert mod.grade({**_rec(), "daily_change_pct": None}) == "data_missing"


def test_short_and_missing_guards():
    assert mod.grade({**_rec(), "side": "short"}) == "side_skip"
    assert mod.grade({**_rec(), "coin": None}) == "no_coin"
    assert mod.grade({"coin": "AAA", "side": "long"}) == "bad_timestamp"


def test_mature_vocabulary_is_win_loss():
    # Pin the grader contract: shadow_grade._window_stats only counts win/loss.
    assert mod.MATURE_OUTCOMES == ("win", "loss")


def test_iter_files_includes_rotated(tmp_path):
    primary = tmp_path / "ext.jsonl"
    primary.write_text("{}")
    (tmp_path / "ext.jsonl.1").write_text("{}")
    out = mod._iter_files(str(primary))
    assert set(out) == {str(primary), str(tmp_path / "ext.jsonl.1")}


def _run_main(monkeypatch, file_path, write=False):
    monkeypatch.setattr(sys, "argv", ["x", "--file", str(file_path)]
                        + (["--write"] if write else []))
    return mod.main()


def test_main_writes_win_loss_and_skips_graded(tmp_path, monkeypatch):
    # Two rows: one would-block chase (up -> win), one below-cap (down -> loss).
    up = [100.0] * 200; up[73] = 110.0
    down = [100.0] * 200; down[73] = 90.0

    calls = {"n": 0}
    tapes = [up, down]

    def _post(path, payload, *a, **k):
        # Distinguish by coin: AAA -> up tape, BBB -> down tape.
        tape = tapes[0] if payload["req"]["coin"] == "AAA" else tapes[1]
        start = int(payload["req"]["startTime"])
        calls["n"] += 1
        return [{"t": start + i * 3600_000, "o": p, "h": p, "l": p, "c": p,
                 "v": "1"} for i, p in enumerate(tape)]

    monkeypatch.setattr(mod, "_http_post", _post)
    p = tmp_path / "ext.jsonl"
    r1 = _rec(); r1["coin"] = "AAA"; r1["ext_would_block"] = True
    r2 = _rec(); r2["coin"] = "BBB"; r2["ext_would_block"] = False
    p.write_text("\n".join(json.dumps(r) for r in (r1, r2)) + "\n")

    assert _run_main(monkeypatch, p, write=True) == 0
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    by_coin = {r["coin"]: r["outcome"] for r in rows}
    assert by_coin == {"AAA": "win", "BBB": "loss"}

    # Second run must not re-grade (no --force); booby-trap the fetcher.
    def _boom(*a, **k):
        raise AssertionError("already-graded rows must not be re-fetched")
    monkeypatch.setattr(mod, "_http_post", _boom)
    assert _run_main(monkeypatch, p, write=False) == 0
