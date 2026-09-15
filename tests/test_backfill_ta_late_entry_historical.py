"""Tests for scripts/backfill_ta_late_entry_historical.py — offline grading for the
ta_late_entry veto arm over its shadow log."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_trader.data import historical_candles as hc
from hermes_trader.data.historical_candles import Candle

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_ta_late_entry_historical",
        ROOT / "scripts" / "backfill_ta_late_entry_historical.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BAR = hc.INTERVAL_MS["4h"]
G0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR)
AS_OF = G0 + 200 * BAR


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "bars.json"))
    yield
    hc.reset_cache()


def _seed(closes):
    hc._BAR_CACHE[("AAA", "4h")] = {
        G0 + i * BAR: Candle(
            t=G0 + i * BAR, o=closes[i], h=closes[i], l=closes[i], c=closes[i], v=1.0
        )
        for i in range(len(closes))
    }


def _rec(ts=G0 + 123456, **kw):
    rec = {
        "coin": "AAA",
        "timestamp": ts,
        "side": "long",
        "blocked": True,
        "entry_px": 100.0,
        "trade_notional_usd": 50.0,
    }
    rec.update(kw)
    return rec


def test_b1_grid_parity_win_and_loss():
    mod = _load_module()
    # t0 falls inside bar idx1 (B1); entry recorded at 100, exit at B1 close.
    _seed([100.0, 105.0])
    rec = _rec()
    assert mod.grade_record(rec, as_of_ms=AS_OF) == "win"
    assert rec["entry_px"] == 100.0
    assert rec["entry_px_source"] == "recorded"
    assert rec["exit_px"] == 105.0
    assert rec["hold_bars"] == mod.HOLD_BARS
    assert rec["pnl_pct"] == pytest.approx(4.95)
    assert rec["mae_pct"] == pytest.approx(0.0)  # gross mae, no fee
    assert rec["pnl_usd"] == pytest.approx(2.475, abs=0.01)

    _seed([100.0, 95.0])
    rec2 = _rec()
    assert mod.grade_record(rec2, as_of_ms=AS_OF) == "loss"
    assert rec2["pnl_pct"] == pytest.approx(-5.05)
    assert rec2["mae_pct"] == pytest.approx(-5.0)
    assert rec2["pnl_usd"] == pytest.approx(-2.525, abs=0.01)


def test_entry_falls_back_to_b0_close_when_entry_px_bad():
    mod = _load_module()
    _seed([100.0, 105.0])
    rec = _rec(entry_px=0.0)
    assert mod.grade_record(rec, as_of_ms=AS_OF) == "win"
    assert rec["entry_px"] == 100.0
    assert rec["entry_px_source"] == "b0_close"


def test_iso_timestamp_accepted():
    mod = _load_module()
    _seed([100.0, 105.0])
    iso = datetime.fromtimestamp((G0 + 123456) / 1000, tz=timezone.utc).isoformat()
    rec = _rec(ts=None)
    rec["timestamp"] = iso
    assert mod.grade_record(rec, as_of_ms=AS_OF) == "win"
    assert rec["pnl_pct"] == pytest.approx(4.95)


def test_guards():
    mod = _load_module()
    _seed([100.0, 105.0])
    assert mod.grade_record({"timestamp": G0}, as_of_ms=AS_OF) == "no_coin"
    assert mod.grade_record(_rec(timestamp="x"), as_of_ms=AS_OF) == "bad_timestamp"
    # Only one bar: no B1 at/after t0.
    hc._BAR_CACHE[("AAA", "4h")] = {
        G0: Candle(t=G0, o=100.0, h=100.0, l=100.0, c=100.0, v=1.0)
    }
    assert mod.grade_record(_rec(), as_of_ms=AS_OF) == "no_future_bars"
    # Unknown coin: no cached bars at all.
    assert mod.grade_record(_rec(coin="BBB"), as_of_ms=AS_OF) == "no_future_bars"


def test_run_grades_only_veto_records(tmp_path):
    mod = _load_module()
    _seed([100.0, 105.0])
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    veto = _rec()
    not_blocked = _rec(ts=G0 + 234567, blocked=False)
    missing_blocked = _rec(ts=G0 + 345678)
    del missing_blocked["blocked"]
    live.write_text(
        "\n".join(json.dumps(r) for r in (veto, not_blocked, missing_blocked)) + "\n",
        encoding="utf-8",
    )
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["input"] == 3
    assert stats["skipped_not_veto"] == 2
    assert stats["win"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "win"
    live_rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all("outcome" not in r for r in live_rows)


def test_run_skips_terminal_unless_forced(tmp_path):
    mod = _load_module()
    _seed([100.0, 105.0])
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    live.write_text(json.dumps(_rec(outcome="loss")) + "\n", encoding="utf-8")
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["skipped_terminal"] == 1
    assert not out.exists()

    stats = mod.run(file=str(live), out=str(out), write=True, force=True, as_of_ms=AS_OF)
    assert stats["win"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["outcome"] == "win"


def test_run_omits_pnl_usd_without_notional(tmp_path):
    mod = _load_module()
    _seed([100.0, 105.0])
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    rec = _rec()
    del rec["trade_notional_usd"]
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["win"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "pnl_usd" not in rows[0]


def test_dedupes_across_rotations(tmp_path):
    mod = _load_module()
    _seed([100.0, 105.0])
    live = tmp_path / "live.jsonl"
    live.with_name(live.name + ".1").write_text(json.dumps(_rec()) + "\n", encoding="utf-8")
    live.write_text(json.dumps(_rec()) + "\n", encoding="utf-8")
    stats = mod.run(file=str(live), out=str(tmp_path / "out.jsonl"), write=True, as_of_ms=AS_OF)
    assert stats["input"] == 1
    assert stats["win"] == 1


def test_refuse_out_equals_file(monkeypatch, tmp_path):
    mod = _load_module()
    f = str(tmp_path / "live.jsonl")
    monkeypatch.setattr(sys, "argv", ["x", "--file", f, "--out", f])
    assert mod.main() == 2


def test_arm_constants():
    mod = _load_module()
    assert mod.ARM == "ta_late_entry"
    assert mod.INTERVAL == "4h"
    assert mod.HOLD_BARS == 2
