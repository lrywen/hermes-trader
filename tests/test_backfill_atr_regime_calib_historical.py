"""Offline tests for the atr_regime_calib historical pilot backfiller.

Pins:
  * geometry parity with reconcile_change_arms_shadow.reconcile_arm (atr
    branch): entry at the open of the first bar to open strictly after the
    signal; bar-low stop walk over HOLD_BARS; edge = v1 net - v2 net;
  * stop-touch vs window-close exit reasons;
  * point-in-time: as_of inside the window -> immature; past the window with
    missing bars -> no_future_bars;
  * guards (no_coin / bad_timestamp / not_material);
  * idempotent isolated product (never the live file), rotation dedupe,
    terminal-skip, --out==--file refusal.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "backfill_atr_hist",
    _REPO / "scripts" / "backfill_atr_regime_calib_historical.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_atr_hist"] = mod
_spec.loader.exec_module(mod)

from hermes_trader.data import historical_candles as hc  # noqa: E402

STEP = hc.INTERVAL_MS["1h"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "cache.json"))
    yield
    hc.reset_cache()


def _mkbar(t, o=100.0, h=None, l=None, c=None):
    from hermes_trader.models.types import Candle
    h = o if h is None else h
    l = o if l is None else l
    c = o if c is None else c
    return Candle(t=t, o=o, h=h, l=l, c=c, v=1.0)


def _seed(bars_by_t):
    """Populate the data layer cache directly (bypasses network)."""
    for t, bar in bars_by_t.items():
        hc._BAR_CACHE.setdefault(("AAA", "1h"), {})[t] = bar


def _flat_bars(grid, n=30, o=100.0, c=None, low=None):
    """n hole-free 1h bars from the grid; flat OHLC unless overridden."""
    out = {}
    for i in range(n):
        t = grid + i * STEP
        out[t] = _mkbar(t, o=o, l=(o if low is None else low),
                        c=(o if c is None else c))
    return out


def _rec(grid, coin="AAA", outcome=None, would_change=True,
         raw=2.0, calib=1.0):
    return {"ts": grid, "coin": coin, "mode": "shadow",
            "atr_pct": 1.2, "atr_hist_mean_pct": 1.0, "ratio": 1.2,
            "vol_regime": "high", "factor": 0.5, "core_stop_pct": raw,
            "trend_regime": "up", "raw_stop_pct": raw,
            "calibrated_stop_pct": calib, "would_change": would_change,
            "outcome": outcome}


def test_window_close_both_legs_edge_zero():
    """Both stops too wide to touch: both legs exit at the window close, so
    v1-v2 edge is 0 -> outcome loss (arm not shown harmful on this record)."""
    grid = 1_700_000_000_000
    grid -= grid % STEP
    # entry bar opens at grid+STEP with o=100; all lows at 99.5 (0.5% adverse,
    # shallower than both the 2% and 1% stops so neither touches); exit at
    # the close of bar entry+24.
    bars = _flat_bars(grid, n=30, o=100.0, low=99.5, c=104.0)
    _seed(bars)
    r = _rec(grid, raw=2.0, calib=1.0)
    as_of = grid + 40 * STEP
    assert mod.grade_record(r, as_of_ms=as_of) == "loss"
    # entry bar is the first t > ts: grid + STEP, entry at its open.
    assert r["cf_entry_px"] == pytest.approx(100.0, abs=1e-9)
    assert r["cf_entry"] == "bar_open"
    assert r["cf_side"] == "long"
    assert r["cf_side_default"] is True
    assert r["cf_hold_bars"] == mod.HOLD_BARS
    # both legs exit at the window close (bar entry+24 = grid+25*STEP, c=104)
    assert r["cf_v1_exit_reason"] == "window_close"
    assert r["cf_v2_exit_reason"] == "window_close"
    fee = mod.ROUND_TRIP_FEE_BPS / 10000.0
    exp_net = (0.04 - fee) * 100.0
    assert r["cf_v1_pnl_pct"] == pytest.approx(exp_net, abs=1e-4)
    assert r["cf_v2_pnl_pct"] == pytest.approx(exp_net, abs=1e-4)
    assert r["pnl_pct"] == pytest.approx(0.0, abs=1e-4)
    # provenance tags
    assert r["outcome_source"] == "historical_replay"
    assert r["fee_bps"] == 5.0
    assert r["entry_px_source"] == "bar_open"


def test_tighter_v2_stop_touched_arm_harmful():
    """v2 (calibrated, tighter) stop is touched but v1 is not: v1 rides to a
    profitable window close while v2 is stopped out -> edge > 0 -> win
    (calibration forgoes return => arm hurts)."""
    grid = 1_700_000_000_000
    grid -= grid % STEP
    # low of every bar after entry dips to 98.5 (1.5% adverse): v2 (1.0%)
    # touches, v1 (2.0%) does not. Window close rides to 104.
    bars = _flat_bars(grid, n=30, o=100.0, low=98.5, c=104.0)
    _seed(bars)
    r = _rec(grid, raw=2.0, calib=1.0)
    as_of = grid + 40 * STEP
    assert mod.grade_record(r, as_of_ms=as_of) == "win"
    assert r["cf_v1_exit_reason"] == "window_close"
    assert r["cf_v2_exit_reason"] == "stop"
    fee = mod.ROUND_TRIP_FEE_BPS / 10000.0
    exp_net1 = (0.04 - fee) * 100.0          # +4% ride, minus fee
    exp_net2 = (-0.01 - fee) * 100.0         # stopped at -1%, minus fee
    assert r["cf_v1_pnl_pct"] == pytest.approx(exp_net1, abs=1e-4)
    assert r["cf_v2_pnl_pct"] == pytest.approx(exp_net2, abs=1e-4)
    assert r["pnl_pct"] == pytest.approx(exp_net1 - exp_net2, abs=1e-4)
    assert r["pnl_pct"] > 0  # edge > 0 == win == arm-harmful


def test_immature_inside_window_then_matures():
    grid = 1_700_000_000_000
    grid -= grid % STEP
    bars = _flat_bars(grid, n=30, o=100.0, low=99.5, c=104.0)
    _seed(bars)
    r = _rec(grid)
    # as_of before the exit bar (grid+25*STEP) closes: entry+24 not yet closed
    early = grid + 20 * STEP
    assert mod.grade_record(r, as_of_ms=early) == "immature"
    # past the full window -> mature
    r2 = _rec(grid)
    assert mod.grade_record(r2, as_of_ms=grid + 40 * STEP) == "loss"


def test_guards():
    grid = 1_700_000_000_000
    grid -= grid % STEP
    # no_coin
    assert mod.grade_record({"ts": grid}, as_of_ms=grid + 40 * STEP) == \
        "no_coin"
    # bad_timestamp
    assert mod.grade_record({"coin": "AAA"}, as_of_ms=grid + 40 * STEP) == \
        "bad_timestamp"
    # not_material: would_change is not True (terminal, no fetch needed)
    r = _rec(grid, would_change=False)
    assert mod.grade_record(r, as_of_ms=grid + 40 * STEP) == "not_material"
    assert r["outcome"] == "not_material"
    # no_future_bars: cached but EMPTY series, as_of past the full window.
    hc._BAR_CACHE[("AAA", "1h")] = {}
    assert mod.grade_record(_rec(grid), as_of_ms=grid + 400 * STEP) == \
        "no_future_bars"


def test_run_writes_isolated_idempotent_product(tmp_path):
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_flat_bars(grid, n=30, o=100.0, low=98.5, c=104.0))
    live = tmp_path / "atr_shadow.jsonl"
    live.write_text(json.dumps(_rec(grid)) + "\n")
    out = tmp_path / "atr.backfill.jsonl"

    stats = mod.run(file=str(live), out=str(out), write=True,
                    as_of_ms=grid + 40 * STEP)
    assert stats["win"] == 1 and stats["loss"] == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["outcome_source"] == "historical_replay"
    # live file untouched
    assert json.loads(live.read_text())["outcome"] is None

    # Re-run: live still outcome=None -> regraded, merged on identity -> 1 row
    stats2 = mod.run(file=str(live), out=str(out), write=True,
                     as_of_ms=grid + 40 * STEP)
    rows2 = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows2) == 1
    assert stats2["written"] == 1


def test_run_skips_already_terminal_in_live(tmp_path):
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_flat_bars(grid, n=30, o=100.0, low=98.5, c=104.0))
    live = tmp_path / "atr_shadow.jsonl"
    live.write_text(json.dumps(_rec(grid, outcome="loss")) + "\n")
    out = tmp_path / "atr.backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True,
                    as_of_ms=grid + 40 * STEP)
    assert stats["skipped_terminal"] == 1
    assert not out.exists()


def test_dedupes_across_rotations(tmp_path):
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_flat_bars(grid, n=30, o=100.0, low=98.5, c=104.0))
    live = tmp_path / "atr_shadow.jsonl"
    live.write_text(json.dumps(_rec(grid)) + "\n")
    rot = tmp_path / "atr_shadow.jsonl.1"
    rot.write_text(json.dumps(_rec(grid)) + "\n")  # same signal
    out = tmp_path / "atr.backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True,
                    as_of_ms=grid + 40 * STEP)
    assert stats["input"] == 1
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["origin_file"] in {"atr_shadow.jsonl", "atr_shadow.jsonl.1"}


def test_refuse_out_equals_file(tmp_path, monkeypatch):
    f = str(tmp_path / "same.jsonl")
    monkeypatch.setattr(sys, "argv", ["x", "--file", f, "--out", f])
    assert mod.main() == 2


def test_arm_constant_is_atr_only():
    assert mod.ARM == "atr_regime_calib"
    assert mod.HOLD_BARS == 24
