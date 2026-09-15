"""Tests for scripts/backfill_relax_tier_historical.py — offline grading for the
relax_tier arm over the shared ta_late_entry shadow log."""

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
        "backfill_relax_tier_historical",
        ROOT / "scripts" / "backfill_relax_tier_historical.py",
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


def _seed(prices):
    hc._BAR_CACHE[("AAA", "4h")] = {
        G0 + i * BAR: Candle(
            t=G0 + i * BAR, o=prices[i], h=prices[i], l=prices[i], c=prices[i], v=1.0
        )
        for i in range(len(prices))
    }


def _rec(ts=G0, **kw):
    rec = {
        "coin": "AAA",
        "timestamp": ts,
        "side": "long",
        "layer": "gate",
        "blocked": False,
        "entry_px": 100.0,
        "rt_relax45_would_block": True,
    }
    rec.update(kw)
    return rec


def _seed_win():
    prices = [100.0] * 20
    prices[19] = 105.0  # fwd72h net +4.95 -> win
    _seed(prices)


def test_grid_parity_win_and_loss():
    mod = _load_module()
    prices = [100.0] * 20
    prices[2] = 101.0
    prices[3] = 99.0
    prices[7] = 102.0
    prices[19] = 105.0
    _seed(prices)
    rec = _rec()
    assert mod.grade_record(rec, as_of_ms=AS_OF) == "win"
    assert rec["rt_forward"]["fwd6h_pct"] == pytest.approx(0.95)
    assert rec["rt_forward"]["fwd24h_pct"] == pytest.approx(1.95)
    assert rec["rt_forward"]["fwd72h_pct"] == pytest.approx(4.95)
    assert rec["rt_forward"]["mae_pct"] == pytest.approx(-1.05)
    assert rec["rt_pnl_pct"] == pytest.approx(4.95)
    assert rec["rt_outcome"] == "win"
    assert rec["rt_outcome_source"] == "historical_replay"

    rec2 = _rec()
    prices[19] = 95.0
    _seed(prices)
    assert mod.grade_record(rec2, as_of_ms=AS_OF) == "loss"


def test_immature_before_72h_bar_closes():
    mod = _load_module()
    _seed([100.0] * 19)  # grid + 72h bar (index 19) not yet closed at as_of
    rec = _rec()
    assert mod.grade_record(rec, as_of_ms=G0 + 19 * BAR) == "immature"
    assert rec["rt_forward"]["fwd6h_pct"] is not None
    assert rec["rt_forward"]["fwd72h_pct"] is None
    assert "rt_outcome" not in rec


def test_guards():
    mod = _load_module()
    _seed([100.0] * 20)
    assert mod.grade_record({"timestamp": G0}, as_of_ms=AS_OF) == "no_coin"
    # entry_px is validated before timestamp parsing.
    assert mod.grade_record(_rec(timestamp="x", entry_px=0.0), as_of_ms=AS_OF) == "no_entry_px"
    assert mod.grade_record(_rec(entry_px="x"), as_of_ms=AS_OF) == "no_entry_px"
    assert mod.grade_record(_rec(timestamp="x"), as_of_ms=AS_OF) == "bad_timestamp"
    assert mod.grade_record(_rec(coin="BBB"), as_of_ms=AS_OF) == "no_future_bars"


def test_run_grades_only_eligible_records(tmp_path):
    mod = _load_module()
    _seed_win()
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    eligible = _rec()
    blocked = _rec(ts=G0 + BAR, blocked=True)
    prefilter = _rec(ts=G0 + 2 * BAR, layer="prefilter")
    no_rt_fields = _rec(ts=G0 + 3 * BAR, rt_relax45_would_block=None)
    live.write_text(
        "\n".join(json.dumps(r) for r in (eligible, blocked, prefilter, no_rt_fields)) + "\n",
        encoding="utf-8",
    )
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["input"] == 4
    assert stats["skipped_ineligible"] == 3
    assert stats["win"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["rt_graded"] is True
    # Live file untouched.
    live_rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all("rt_outcome" not in r for r in live_rows)


def test_run_skips_rt_graded_unless_forced(tmp_path):
    mod = _load_module()
    _seed_win()
    live = tmp_path / "live.jsonl"
    out = tmp_path / "out.jsonl"
    live.write_text(json.dumps(_rec(rt_graded=True)) + "\n", encoding="utf-8")
    stats = mod.run(file=str(live), out=str(out), write=True, as_of_ms=AS_OF)
    assert stats["skipped_terminal"] == 1
    assert not out.exists()

    stats = mod.run(file=str(live), out=str(out), write=True, force=True, as_of_ms=AS_OF)
    assert stats["win"] == 1
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["rt_graded"] is True


def test_dedupes_across_rotations(tmp_path):
    mod = _load_module()
    _seed_win()
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
    assert mod.ARM == "relax_tier"
    assert mod.INTERVAL == "4h"
    assert mod.FWD_HOURS == (6, 24, 72)
    assert mod.GRADED_FLAG == "rt_graded"
