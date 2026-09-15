"""Offline tests for the xs_reversal historical pilot backfiller (no network).

Pins:
  * fixed-window grid parity with reconcile_xs_reversal_shadow (fwd hh close
    == bar that opens at signal-grid + hh);
  * point-in-time: as_of before the 72h bar prints -> immature;
  * net-of-5bps verdict + gross/net flips;
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
    "backfill_xs_hist",
    _REPO / "scripts" / "backfill_xs_reversal_historical.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_xs_hist"] = mod
_spec.loader.exec_module(mod)

from hermes_trader.data import historical_candles as hc  # noqa: E402

STEP = hc.INTERVAL_MS["1h"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "cache.json"))
    yield
    hc.reset_cache()


def _seed(prices_by_t):
    """Populate the data layer cache directly (bypasses network)."""
    for t, p in prices_by_t.items():
        hc._BAR_CACHE.setdefault(("AAA", "1h"), {})[t] = _mkbar(t, c=p)


def _mkbar(t, c=100.0):
    from hermes_trader.models.types import Candle
    return Candle(t=t, o=c, h=c, l=c, c=c, v=1.0)


def _rec(grid, entry=100.0, coin="AAA", outcome=None, origin=None):
    r = {"timestamp": grid, "coin": coin, "side": "long",
         "entry_px": entry, "is_candidate": True,
         "macro_regime": "NEUTRAL", "outcome": outcome}
    if origin:
        r["_origin_file"] = origin
    return r


def _prices(grid, at72=None, at24=None, at168=None):
    prices = {grid + i * STEP: 100.0 for i in range(-1, 180)}
    if at24 is not None:
        prices[grid + 24 * STEP] = at24
    if at72 is not None:
        prices[grid + 72 * STEP] = at72
    if at168 is not None:
        prices[grid + 168 * STEP] = at168
    return prices


def test_grid_parity_win_and_forward_points():
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_prices(grid, at72=105.0, at24=101.0, at168=102.0))
    r = _rec(grid)
    as_of = grid + 200 * STEP
    assert mod.grade_record(r, as_of_ms=as_of) == "win"
    assert r["forward"]["fwd72h_pct"] == pytest.approx(5.0, abs=1e-6)
    assert r["forward"]["fwd24h_pct"] == pytest.approx(1.0, abs=1e-6)
    assert r["forward"]["fwd168h_pct"] == pytest.approx(2.0, abs=1e-6)
    # provenance tags
    assert r["outcome_source"] == "historical_replay"
    assert r["fee_bps"] == 5.0
    assert r["entry_px_source"] == "signal_bar_close"


def test_net_of_fees_verdict_and_flip():
    grid = 1_700_000_000_000
    grid -= grid % STEP
    # gross +0.03% win, minus 5bps -> net loss
    _seed(_prices(grid, at72=100.03))
    r = _rec(grid)
    assert mod.grade_record(r, as_of_ms=grid + 200 * STEP) == "win"
    assert r["outcome_net"] == "loss"
    assert r["pnl_pct"] == pytest.approx(0.03, abs=1e-6)
    assert r["pnl_pct_net"] == pytest.approx(0.03 - 0.05, abs=1e-6)


def test_immature_before_72h_bar_closes():
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_prices(grid, at72=105.0))
    r = _rec(grid)
    # as_of at the open of the 72h bar: it has not closed yet (closes at +73h)
    assert mod.grade_record(r, as_of_ms=grid + 72 * STEP) == "immature"
    assert r["forward"]["fwd72h_pct"] is None
    # one hour later the bar closes
    r2 = _rec(grid)
    assert mod.grade_record(r2, as_of_ms=grid + 73 * STEP) == "win"


def test_guards():
    assert mod.grade_record({"coin": "AAA", "entry_px": 0},
                            as_of_ms=10**15) == "no_entry_px"
    assert mod.grade_record({"coin": "AAA", "entry_px": 100},
                            as_of_ms=10**15) == "bad_timestamp"
    assert mod.grade_record({"entry_px": 100, "timestamp": 10**15},
                            as_of_ms=10**15) == "no_coin"
    # no_future_bars: seed an EMPTY cached series so no fetch is attempted.
    grid = 1_700_000_000_000
    grid -= grid % STEP
    hc._BAR_CACHE[("AAA", "1h")] = {}  # cached but empty → no_future_bars
    assert mod.grade_record(_rec(grid), as_of_ms=grid + 400 * STEP) == \
        "no_future_bars"


def test_run_writes_isolated_idempotent_product(tmp_path):
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_prices(grid, at72=105.0))
    live = tmp_path / "xs_shadow.jsonl"
    live.write_text(json.dumps(_rec(grid)) + "\n")
    out = tmp_path / "xs.backfill.jsonl"

    stats = mod.run(file=str(live), out=str(out), write=True,
                    as_of_ms=grid + 200 * STEP)
    assert stats["win"] == 1 and stats["loss"] == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["outcome_source"] == "historical_replay"
    # live file untouched
    assert json.loads(live.read_text())["outcome"] is None

    # Re-run: terminal rows in the *live* file are skipped, but the product
    # already holds the graded row — dedupe keeps exactly one row.
    stats2 = mod.run(file=str(live), out=str(out), write=True,
                     as_of_ms=grid + 200 * STEP)
    # live still has outcome=None → regraded and merged on identity → 1 row
    rows2 = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows2) == 1
    assert stats2["written"] == 1


def test_run_skips_already_terminal_in_live(tmp_path):
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_prices(grid, at72=105.0))
    live = tmp_path / "xs_shadow.jsonl"
    live.write_text(json.dumps(_rec(grid, outcome="loss")) + "\n")
    out = tmp_path / "xs.backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True,
                    as_of_ms=grid + 200 * STEP)
    assert stats["skipped_terminal"] == 1
    assert not out.exists()


def test_dedupes_across_rotations(tmp_path):
    grid = 1_700_000_000_000
    grid -= grid % STEP
    _seed(_prices(grid, at72=105.0))
    live = tmp_path / "xs_shadow.jsonl"
    live.write_text(json.dumps(_rec(grid)) + "\n")
    rot = tmp_path / "xs_shadow.jsonl.1"
    rot.write_text(json.dumps(_rec(grid)) + "\n")  # same signal
    out = tmp_path / "xs.backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True,
                    as_of_ms=grid + 200 * STEP)
    assert stats["input"] == 1
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["origin_file"] in {"xs_shadow.jsonl", "xs_shadow.jsonl.1"}


def test_refuse_out_equals_file(tmp_path, monkeypatch):
    f = str(tmp_path / "same.jsonl")
    monkeypatch.setattr(sys, "argv", ["x", "--file", f, "--out", f])
    assert mod.main() == 2


def test_arm_constant_is_xs_only():
    # Pilot whitelist contract.
    assert mod.ARM == "xs_reversal"
    assert mod.FWD_HOURS == (24, 72, 168)
