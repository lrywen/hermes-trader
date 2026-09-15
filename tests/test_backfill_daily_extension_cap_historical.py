"""Tests for scripts/backfill_daily_extension_cap_historical.py."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_trader.data import historical_candles as hc
from hermes_trader.data.historical_candles import Candle

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "backfill_daily_extension_cap_historical",
    ROOT / "scripts" / "backfill_daily_extension_cap_historical.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

STEP = hc.INTERVAL_MS["1h"]
G0 = 1_700_000_000_000 - (1_700_000_000_000 % STEP)
AS_OF = G0 + 200 * STEP


def _seed(prices, coin="AAA"):
    hc._BAR_CACHE[(coin, "1h")] = {
        G0 + i * STEP: Candle(t=G0 + i * STEP, o=p, h=p, l=p, c=p, v=1.0)
        for i, p in enumerate(prices)
    }


def _rec(ts=G0, **kw):
    rec = {"coin": "AAA", "timestamp": ts, "side": "long",
           "state": "shadow", "daily_change_pct": 3.5}
    rec.update(kw)
    return rec


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "bars.json"))
    yield
    hc.reset_cache()


def test_grid_parity_win_and_loss():
    prices = [100.0] * 169
    prices[24] = 103.0
    prices[72] = 105.0
    prices[168] = 98.0
    _seed(prices)
    # Mid-bar signal ts must anchor to the signal bar grid (grid0).
    rec = _rec(ts=G0 + 123456)
    assert mod.grade_record(rec, as_of_ms=AS_OF) == "win"
    assert rec["entry_px"] == 100.0
    assert rec["entry_px_source"] == "signal_bar_close"
    assert rec["forward"]["fwd24h_pct"] == 3.0
    assert rec["forward"]["fwd72h_pct"] == 5.0
    assert rec["forward"]["fwd72h_px"] == 105.0
    assert rec["forward"]["fwd168h_pct"] == -2.0
    assert rec["pnl_pct"] == 5.0
    assert rec["outcome_source"] == "historical_replay"

    loss_prices = [100.0] * 169
    loss_prices[72] = 95.0
    _seed(loss_prices)
    loss = _rec(ts=G0 + 123456)
    assert mod.grade_record(loss, as_of_ms=AS_OF) == "loss"
    assert loss["pnl_pct"] == -5.0
    assert loss["forward"]["fwd72h_pct"] == -5.0


def test_immature_before_primary_horizon_bar_closes():
    _seed([100.0] * 73)  # grid bars 0..72 exist on disk
    rec = _rec()
    # Bar opened at grid0+72h has NOT closed at as_of=grid0+72h (PIT).
    outcome = mod.grade_record(rec, as_of_ms=G0 + 72 * STEP)
    assert outcome == "immature"
    assert rec["entry_px"] == 100.0
    assert rec["forward"]["fwd24h_pct"] == 0.0
    assert rec["forward"]["fwd72h_pct"] is None
    assert "outcome" not in rec
    assert "outcome_source" not in rec


def test_guards():
    assert mod.grade_record({"timestamp": G0}, as_of_ms=AS_OF) == "no_coin"
    assert mod.grade_record(_rec(timestamp="bad"), as_of_ms=AS_OF) == \
        "bad_timestamp"
    assert mod.grade_record(_rec(side="short"), as_of_ms=AS_OF) == "side_skip"
    r = _rec(state="data_missing")
    assert mod.grade_record(r, as_of_ms=AS_OF) == "data_missing"
    r2 = _rec(daily_change_pct=None)
    assert mod.grade_record(r2, as_of_ms=AS_OF) == "data_missing"
    # No bars at all for the coin.
    hc._BAR_CACHE[("AAA", "1h")] = {}
    assert mod.grade_record(_rec(), as_of_ms=AS_OF) == "no_future_bars"
    # Bars exist but the signal bar itself is missing.
    _seed([100.0] * 30)
    del hc._BAR_CACHE[("AAA", "1h")][G0 + 5 * STEP]
    r3 = _rec(ts=G0 + 5 * STEP + 999)
    assert mod.grade_record(r3, as_of_ms=AS_OF) == "no_entry_px"


def test_run_writes_isolated_idempotent_product(tmp_path):
    prices = [100.0] * 169
    prices[72] = 105.0
    _seed(prices)
    live = tmp_path / "daily_extension_cap_shadow.jsonl"
    live.write_text(json.dumps(_rec()) + "\n")
    out = tmp_path / "backfill.jsonl"

    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["win"] == 1 and stats["loss"] == 0
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "win"
    assert rows[0]["outcome_source"] == "historical_replay"
    assert rows[0]["origin_file"] == live.name
    # Live file untouched.
    assert "outcome" not in json.loads(live.read_text())

    # Re-run: live row still ungraded → regraded, merged on identity → 1 row.
    stats2 = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    rows2 = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows2) == 1
    assert stats2["written"] == 1


def test_run_guard_records_not_produced(tmp_path):
    _seed([100.0] * 169)
    live = tmp_path / "shadow.jsonl"
    live.write_text(json.dumps(_rec(side="short")) + "\n")
    out = tmp_path / "backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["skipped_guard"] == 1
    assert stats["produced"] == 0
    assert not out.exists()


def test_run_skips_already_terminal_unless_forced(tmp_path):
    prices = [100.0] * 169
    prices[72] = 105.0
    _seed(prices)
    live = tmp_path / "shadow.jsonl"
    live.write_text(json.dumps(_rec(outcome="loss")) + "\n")
    out = tmp_path / "backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["skipped_terminal"] == 1
    assert not out.exists()
    # --force re-grades against history (loss -> win under this replay).
    stats2 = mod.run(file=str(live), out=str(out), write=True, force=True,
                     as_of_ms=AS_OF)
    assert stats2["win"] == 1
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert rows[0]["outcome"] == "win"


def test_dedupes_across_rotations(tmp_path):
    prices = [100.0] * 169
    prices[72] = 105.0
    _seed(prices)
    live = tmp_path / "shadow.jsonl"
    live.write_text(json.dumps(_rec()) + "\n")
    rot = tmp_path / "shadow.jsonl.1"
    rot.write_text(json.dumps(_rec()) + "\n")  # same signal identity
    out = tmp_path / "backfill.jsonl"
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["input"] == 1
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["origin_file"] in {live.name, rot.name}


def test_refuse_out_equals_file(tmp_path, monkeypatch):
    f = str(tmp_path / "same.jsonl")
    monkeypatch.setattr(sys, "argv", ["x", "--file", f, "--out", f])
    assert mod.main() == 2


def test_arm_constant():
    assert mod.ARM == "daily_extension_cap"
    assert mod.INTERVAL == "1h"
    assert mod.FWD_HOURS == (24, 72, 168)
    assert mod.PRIMARY_FWD_HOUR == 72
