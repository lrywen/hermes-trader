"""Tests for scripts/backfill_pullback_historical.py — offline DSL-exit replay for the
pullback arm over its shadow log."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_trader.data import historical_candles as hc
from hermes_trader.data.historical_candles import Candle

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_pullback_historical",
        ROOT / "scripts" / "backfill_pullback_historical.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STEP = hc.INTERVAL_MS["1h"]
G0 = 1_700_000_000_000 - (1_700_000_000_000 % STEP)
AS_OF = G0 + 10 * STEP
DSL = {"dsl_exit": {"max_loss_pct": 3.0, "protect_pct": 2.0, "retrace_threshold": 0.5}}


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "bars.json"))
    yield
    hc.reset_cache()


def _seed(ohlc):
    hc._BAR_CACHE[("AAA", "1h")] = {
        G0 + i * STEP: Candle(
            t=G0 + i * STEP, o=o, h=h, l=l, c=c, v=1.0
        )
        for i, (o, h, l, c) in enumerate(ohlc)
    }


def _rec(ts=G0 + 123456, **kw):  # t0 inside bar idx1; walk starts at idx2
    rec = {"coin": "AAA", "timestamp": ts, "entry_px": 100.0}
    rec.update(kw)
    return rec


def test_trailing_stop_win():
    mod = _load_module()
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),  # entry bar
        (100.0, 103.0, 100.0, 102.0),  # peak -> 103 after checks
        (102.4, 102.5, 101.4, 101.8),  # floor 101.5 breached
    ])
    rec = _rec()
    assert mod.grade_record(rec, dsl_cfg=DSL, as_of_ms=AS_OF) == "win"
    assert rec["exit_reason"] == "trailing_stop"
    assert rec["exit_px"] == pytest.approx(101.5)
    assert rec["pnl_pct"] == pytest.approx(1.45)


def test_max_loss_stop():
    mod = _load_module()
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
        (99.0, 99.5, 96.9, 97.5),  # stop at 97 hit
        (97.5, 97.5, 97.5, 97.5),
    ])
    rec = _rec()
    assert mod.grade_record(rec, dsl_cfg=DSL, as_of_ms=AS_OF) == "loss"
    assert rec["exit_reason"] == "max_loss 3.0%"
    assert rec["exit_px"] == pytest.approx(97.0)
    assert rec["pnl_pct"] == pytest.approx(-3.05)


def test_canonical_defaults_when_dsl_cfg_empty():
    mod = _load_module()
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
        (99.1, 99.2, 98.9, 99.0),  # canonical stop at 99.0 (flat max_loss 1.0%)
        (99.0, 99.0, 99.0, 99.0),
    ])
    rec = _rec()
    assert mod.grade_record(rec, dsl_cfg={}, as_of_ms=AS_OF) == "loss"
    assert rec["exit_reason"] == "max_loss 1.0%"
    assert rec["pnl_pct"] == pytest.approx(-1.05)


def test_window_end_when_no_stop_hit():
    mod = _load_module()
    _seed([(100.0, 100.0, 100.0, 100.0)] * 5)
    rec = _rec()
    assert mod.grade_record(rec, dsl_cfg=DSL, as_of_ms=AS_OF) == "loss"
    assert rec["exit_reason"] == "window_end"
    assert rec["exit_px"] == pytest.approx(100.0)
    assert rec["pnl_pct"] == pytest.approx(-0.05)


def test_entry_falls_back_to_signal_bar_open():
    mod = _load_module()
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 103.0, 100.0, 102.0),
        (102.4, 102.5, 101.4, 101.8),
    ])
    rec = _rec()
    del rec["entry_px"]
    assert mod.grade_record(rec, dsl_cfg=DSL, as_of_ms=AS_OF) == "win"
    assert rec["entry_px_source"] == "signal_bar_open"
    assert "entry_px" not in rec  # fallback does not write entry_px back
    assert rec["pnl_pct"] == pytest.approx(1.45)


def test_guards():
    mod = _load_module()
    # Entry bar open is zero and no usable entry_px -> no_entry_px.
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (0.0, 100.0, 0.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
    ])
    rec = _rec()
    del rec["entry_px"]
    assert mod.grade_record(rec, dsl_cfg=DSL, as_of_ms=AS_OF) == "no_entry_px"
    # Only 2 bars: idx (1) >= len-2 (0).
    _seed([(100.0, 100.0, 100.0, 100.0)] * 2)
    assert mod.grade_record(_rec(), dsl_cfg=DSL, as_of_ms=AS_OF) == "no_future_bars"
    # Unknown coin: nothing cached.
    assert mod.grade_record(_rec(coin="BBB"), dsl_cfg=DSL, as_of_ms=AS_OF) == "no_future_bars"


def test_run_retries_retryable_outcomes_and_skips_terminal(tmp_path):
    mod = _load_module()
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 103.0, 100.0, 102.0),
        (102.4, 102.5, 101.4, 101.8),
    ])
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    retryable = _rec(outcome="no_entry_px")
    del retryable["entry_px"]  # re-grades via signal_bar_open fallback
    terminal = _rec(ts=G0 + 234567, outcome="win")
    live.write_text(
        json.dumps(retryable) + "\n" + json.dumps(terminal) + "\n", encoding="utf-8"
    )
    stats = mod.run(file=str(live), out=str(out), write=True, dsl_cfg=DSL, as_of_ms=AS_OF)
    assert stats["input"] == 2
    assert stats["skipped_terminal"] == 1
    assert stats["win"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "win"
    assert rows[0]["entry_px_source"] == "signal_bar_open"
    # Live file untouched.
    live_rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert live_rows[0]["outcome"] == "no_entry_px"
    assert "exit_reason" not in live_rows[0]


def test_run_idempotent_write(tmp_path):
    mod = _load_module()
    _seed([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 103.0, 100.0, 102.0),
        (102.4, 102.5, 101.4, 101.8),
    ])
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    live.write_text(json.dumps(_rec()) + "\n", encoding="utf-8")
    stats1 = mod.run(file=str(live), out=str(out), write=True, dsl_cfg=DSL, as_of_ms=AS_OF)
    assert stats1["win"] == 1
    stats2 = mod.run(file=str(live), out=str(out), write=True, dsl_cfg=DSL, as_of_ms=AS_OF)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert stats2["written"] == 1


def test_refuse_out_equals_file(monkeypatch, tmp_path):
    mod = _load_module()
    f = str(tmp_path / "live.jsonl")
    monkeypatch.setattr(sys, "argv", ["x", "--file", f, "--out", f])
    assert mod.main() == 2


def test_arm_constants():
    mod = _load_module()
    assert mod.ARM == "pullback"
    assert mod.INTERVAL == "1h"
    assert mod.HARD_TIMEOUT_BARS == 180
