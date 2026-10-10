"""Tests for scripts/reconcile_gate_outcomes.py (R1 backfill gap)."""
from __future__ import annotations

import json

import pytest

from scripts import reconcile_gate_outcomes as rgo


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    return tmp_path


def _age_rec(ts_ms, would_block):
    return {"ts": ts_ms, "mode": "shadow", "coin": "UNI",
            "would_block": would_block}


def _trend_rec(iso, would_block, price):
    return {"timestamp": iso, "coin": "DOT", "mode": "enforce",
            "trend_would_block": would_block, "price": price}


def test_record_ms_age_millis(setup):
    arm = rgo._ARMS[0]
    assert rgo._record_ms(_age_rec(1791590400000, True), arm) == 1791590400000


def test_record_ms_trend_iso(setup):
    arm = rgo._ARMS[1]
    ms = rgo._record_ms(_trend_rec("2026-10-10T00:00:00Z", True, 1.2), arm)
    assert isinstance(ms, int) and ms > 0


def test_age_decay_grades_with_candle_entry(setup, monkeypatch):
    start = 1791590400123
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 10.0, grid0 + rgo.FWD_HOURS * rgo._STEP: 11.0})
    rec = _age_rec(start, True)
    assert rgo._grade(rec, rgo._ARMS[0]) is True
    # +10% gross well above cost -> win; entry came from candles.
    assert rec["outcome"] == "win"
    assert rec["entry_px"] == 10.0
    assert rec["exit_px"] == 11.0


def test_trend_filter_uses_record_price(setup, monkeypatch):
    iso = "2026-10-10T00:00:00Z"
    start = rgo._record_ms(_trend_rec(iso, True, 5.0), rgo._ARMS[1])
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 9.9, grid0 + rgo.FWD_HOURS * rgo._STEP: 4.0})
    rec = _trend_rec(iso, True, 5.0)
    assert rgo._grade(rec, rgo._ARMS[1]) is True
    # entry = record price 5.0 (not the grid0 candle 9.9); exit 4.0 -> loss.
    assert rec["entry_px"] == 5.0
    assert rec["outcome"] == "loss"


def test_skips_non_hit_records(setup, monkeypatch):
    rec = _age_rec(1791590400000, False)
    assert rgo._grade(rec, rgo._ARMS[0]) is False
    assert "outcome" not in rec


def test_skips_already_graded(setup, monkeypatch):
    rec = _age_rec(1791590400000, True)
    rec["outcome"] = "win"
    assert rgo._grade(rec, rgo._ARMS[0]) is False


def test_forward_bar_missing_means_not_graded(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(rgo, "_bars", lambda coin, s, h: {grid0: 10.0})
    rec = _age_rec(start, True)
    assert rgo._grade(rec, rgo._ARMS[0]) is False
    assert "outcome" not in rec


def test_cost_makes_tiny_gain_a_loss(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    # +0.05% gross, below the 0.09% round-trip cost -> loss.
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 100.0,
                            grid0 + rgo.FWD_HOURS * rgo._STEP: 100.05})
    rec = _age_rec(start, True)
    rgo._grade(rec, rgo._ARMS[0])
    assert rec["outcome"] == "loss"


def test_process_arm_write_persists(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 10.0,
                            grid0 + rgo.FWD_HOURS * rgo._STEP: 12.0})
    f = setup / "signal_age_decay_shadow.jsonl"
    f.write_text(json.dumps(_age_rec(start, True)) + "\n")
    stats = rgo.process_arm(rgo._ARMS[0], write=True)
    assert stats["newly_graded"] == 1
    stamped = json.loads(f.read_text())
    assert stamped["outcome"] == "win"
    # Atomic: no leftover tmp file.
    assert not (setup / "signal_age_decay_shadow.jsonl.tmp").exists()
