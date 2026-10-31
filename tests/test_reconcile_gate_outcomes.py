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


def _arm(name):
    return next(a for a in rgo._ARMS if a["name"] == name)


def test_record_ms_age_millis(setup):
    arm = _arm("per_coin_cooldown")
    assert rgo._record_ms(_age_rec(1791590400000, True), arm) == 1791590400000


def test_record_ms_trend_iso(setup):
    arm = _arm("trend_filter_200ma")
    ms = rgo._record_ms(_trend_rec("2026-10-10T00:00:00Z", True, 1.2), arm)
    assert isinstance(ms, int) and ms > 0


def test_cooldown_grades_with_candle_entry(setup, monkeypatch):
    start = 1791590400123
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 10.0, grid0 + rgo.FWD_HOURS * rgo._STEP: 11.0})
    rec = _age_rec(start, True)
    assert rgo._grade(rec, _arm("per_coin_cooldown")) is True
    # +10% gross well above cost -> win; entry came from candles.
    assert rec["outcome"] == "win"
    assert rec["entry_px"] == 10.0
    assert rec["exit_px"] == 11.0


def test_trend_filter_uses_record_price(setup, monkeypatch):
    iso = "2026-10-10T00:00:00Z"
    arm = _arm("trend_filter_200ma")
    start = rgo._record_ms(_trend_rec(iso, True, 5.0), arm)
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 9.9, grid0 + rgo.FWD_HOURS * rgo._STEP: 4.0})
    rec = _trend_rec(iso, True, 5.0)
    assert rgo._grade(rec, arm) is True
    # entry = record price 5.0 (not the grid0 candle 9.9); exit 4.0 -> loss.
    assert rec["entry_px"] == 5.0
    assert rec["outcome"] == "loss"


def test_skips_non_hit_records(setup, monkeypatch):
    rec = _age_rec(1791590400000, False)
    assert rgo._grade(rec, _arm("per_coin_cooldown")) is False
    assert "outcome" not in rec


def test_skips_already_graded(setup, monkeypatch):
    rec = _age_rec(1791590400000, True)
    rec["outcome"] = "win"
    assert rgo._grade(rec, _arm("per_coin_cooldown")) is False


def test_forward_bar_missing_means_not_graded(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(rgo, "_bars", lambda coin, s, h: {grid0: 10.0})
    rec = _age_rec(start, True)
    assert rgo._grade(rec, _arm("per_coin_cooldown")) is False
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
    rgo._grade(rec, _arm("per_coin_cooldown"))
    assert rec["outcome"] == "loss"


def test_process_arm_write_persists(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 10.0,
                            grid0 + rgo.FWD_HOURS * rgo._STEP: 12.0})
    f = setup / "per_coin_cooldown_shadow.jsonl"
    f.write_text(json.dumps(_age_rec(start, True)) + "\n")
    stats = rgo.process_arm(_arm("per_coin_cooldown"), write=True)
    assert stats["newly_graded"] == 1
    stamped = json.loads(f.read_text())
    assert stamped["outcome"] == "win"
    # Atomic: no leftover tmp file.
    assert not (setup / "per_coin_cooldown_shadow.jsonl.tmp").exists()


# ── P0-2: gate-outcome backfill arms ───────────────────────────────────────


def test_arm_table_covers_six_gates(setup):
    names = {a["name"] for a in rgo._ARMS}
    assert len(rgo._ARMS) == 6
    assert {
        "trend_filter_200ma", "per_coin_cooldown", "daily_extension_cap",
        "regime_direction", "runner_gate", "confidence_decay",
    } == names


def test_runner_gate_grades_refused_candidates(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    monkeypatch.setattr(
        rgo, "_bars",
        lambda coin, s, h: {grid0: 10.0,
                            grid0 + rgo.FWD_HOURS * rgo._STEP: 9.0})
    rec = {"ts": start, "coin": "ACE", "side": "short", "admitted": False,
           "mid_price": None}
    assert rgo._grade(rec, _arm("runner_gate")) is True
    # Recorded mid_price is null -> candle entry; a refused SHORT that falls
    # 10 -> 9 is a win once direction is honoured.
    assert rec["entry_px"] == 10.0
    assert rec["pnl_pct"] > 0
    assert rec["outcome"] == "win"


def test_runner_gate_skips_admitted(setup, monkeypatch):
    monkeypatch.setattr(rgo, "_bars", lambda *a: {})
    rec = {"ts": 1791590400000, "coin": "ACE", "admitted": True}
    assert rgo._grade(rec, _arm("runner_gate")) is False
    assert "outcome" not in rec


def test_per_coin_cooldown_millis_arm(setup):
    assert rgo._record_ms({"ts": 1793407232002}, _arm("per_coin_cooldown")) \
        == 1793407232002


def test_confidence_decay_uses_cf_entry_and_24h(setup, monkeypatch):
    start = 1791590400000
    grid0 = start - (start % rgo._STEP)
    seen = {}

    def fake_bars(coin, s, h):
        seen["horizon"] = h
        return {grid0: 555.0, grid0 + 24 * rgo._STEP: 600.0}

    monkeypatch.setattr(rgo, "_bars", fake_bars)
    rec = {"ts": start, "coin": "XMR", "would_block_gate": True,
           "cf_entry_px": 567.04}
    assert rgo._grade(rec, _arm("confidence_decay")) is True
    assert seen["horizon"] == 24
    assert rec["entry_px"] == 567.04
    assert rec["outcome"] == "win"


def test_immature_exit_bar_is_not_graded(setup, monkeypatch):
    """An unclosed exit bar must not trigger a candle fetch (the 10-min hang)."""
    calls = []
    monkeypatch.setattr(rgo, "_bars", lambda *a: calls.append(a) or {})
    now = rgo._NOW_MS
    recent = now - 2 * rgo._STEP  # exit bar 72h out has not closed yet
    rec = _age_rec(recent, True)
    assert rgo._grade(rec, _arm("per_coin_cooldown")) is False
    assert "outcome" not in rec
    assert calls == []
