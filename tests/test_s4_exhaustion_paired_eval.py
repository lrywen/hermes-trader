"""Tests for scripts/s4_exhaustion_paired_eval.py (S4-2 paired exhaustion eval).

Pins:
  * directional bp convention (long + / short -)
  * paired delta = virtual gross - real gross
  * counterfactual rows without a matching real close are unmatched, not paired
  * n < 30 → inconclusive regardless of point estimate
  * block bootstrap needs >= 2 days for a CI
  * decision rule: CI lower > 0 AND worst month >= 0 AND n >= 30
  * only the two pre-registered signals are evaluated
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "s4_exhaustion_paired_eval.py"
_spec = importlib.util.spec_from_file_location("s4_exhaustion", _SCRIPT)
s4 = importlib.util.module_from_spec(_spec)
sys.modules["s4_exhaustion"] = s4
_spec.loader.exec_module(s4)  # type: ignore[union-attr]


def _book_with_close(pid="p1", coin="BTC", side="long",
                     entry=100.0, exit_px=101.0, account="taker"):
    return {
        "accounts": {
            account: {
                "fills": [
                    {"type": "open", "position_id": pid, "coin": coin,
                     "side": side, "price": entry, "mid_px": entry},
                    {"type": "close", "position_id": pid, "coin": coin,
                     "side": side, "price": exit_px, "reason": "trailing_stop"},
                ],
            },
        },
    }


def _cf(pid, signal, mid, ts=1_700_000_000_000, reason="test"):
    return {"position_id": pid, "signal": signal, "mid": mid, "ts": ts,
            "virtual_exit_ts": ts, "observer_only": True, "reason": reason,
            "account": "taker", "evidence": {}}


def test_directional_bp_long_and_short():
    assert s4.directional_bp(100.0, 101.0, "long") == pytest.approx(100.0)
    assert s4.directional_bp(100.0, 99.0, "short") == pytest.approx(100.0)
    assert s4.directional_bp(100.0, 101.0, "short") == pytest.approx(-100.0)


def test_paired_delta_virtual_minus_real(tmp_path):
    book = _book_with_close(exit_px=101.0)  # real +100bp
    (tmp_path / ".shadow-book.json").write_text(json.dumps(book))
    # virtual exit at 102 → +200bp; delta = +100bp.
    cf = [_cf("p1", "cvd_divergence", 102.0)]
    (tmp_path / ".shadow-exit-counterfactual.jsonl").write_text(
        "\n".join(json.dumps(r) for r in cf))
    result = s4.evaluate(tmp_path)
    arm = result["arms"]["cvd_divergence"]
    assert arm["n"] == 1
    assert arm["delta_bp"] if False else arm["rows"][0]["delta_bp"] == pytest.approx(100.0)
    assert arm["rows"][0]["virtual_gross_bp"] == pytest.approx(200.0)
    assert arm["rows"][0]["real_gross_bp"] == pytest.approx(100.0)


def test_unmatched_virtual_exit_not_paired(tmp_path):
    book = _book_with_close(pid="p1")
    (tmp_path / ".shadow-book.json").write_text(json.dumps(book))
    cf = [_cf("p_unknown", "cvd_divergence", 102.0)]
    (tmp_path / ".shadow-exit-counterfactual.jsonl").write_text(
        "\n".join(json.dumps(r) for r in cf))
    result = s4.evaluate(tmp_path)
    assert result["unmatched_virtual_exits"] == 1
    assert result["arms"]["cvd_divergence"]["n"] == 0
    assert result["arms"]["cvd_divergence"]["status"] == "no_data"


def test_insufficient_n_is_inconclusive(tmp_path):
    # 5 paired rows, all strongly positive — still inconclusive (< 30).
    fills = []
    cfs = []
    for i in range(5):
        pid = f"p{i}"
        fills.append({"type": "open", "position_id": pid, "coin": "BTC",
                      "side": "long", "price": 100.0, "mid_px": 100.0})
        fills.append({"type": "close", "position_id": pid, "coin": "BTC",
                      "side": "long", "price": 100.5, "reason": "hard_timeout"})
        cfs.append(_cf(pid, "phase2_floor_breach", 102.0,
                       ts=1_700_000_000_000 + i * 86_400_000))
    book = {"accounts": {"taker": {"fills": fills}}}
    (tmp_path / ".shadow-book.json").write_text(json.dumps(book))
    (tmp_path / ".shadow-exit-counterfactual.jsonl").write_text(
        "\n".join(json.dumps(r) for r in cfs))
    result = s4.evaluate(tmp_path)
    arm = result["arms"]["phase2_trailing_floor"]
    assert arm["n"] == 5
    assert arm["status"] == "insufficient_n"
    assert arm["decision"] == "inconclusive"


def test_unknown_signal_ignored(tmp_path):
    book = _book_with_close()
    (tmp_path / ".shadow-book.json").write_text(json.dumps(book))
    cf = [_cf("p1", "some_future_signal", 102.0)]
    (tmp_path / ".shadow-exit-counterfactual.jsonl").write_text(
        "\n".join(json.dumps(r) for r in cf))
    result = s4.evaluate(tmp_path)
    assert result["counterfactual_rows_total"] == 1
    assert all(a["n"] == 0 for a in result["arms"].values())


def test_bootstrap_requires_two_day_blocks():
    # All rows on one day → CI is null.
    rows_deltas = [10.0, 20.0, 30.0]
    days = ["2026-11-01"] * 3
    boot = s4.day_block_bootstrap(rows_deltas, days, seed=1)
    assert boot["blocks"] == 1
    assert boot["ci95_bp"] == [None, None]
    # Two days → CI present.
    days2 = ["2026-11-01", "2026-11-01", "2026-11-02"]
    boot2 = s4.day_block_bootstrap(rows_deltas, days2, seed=1)
    assert boot2["blocks"] == 2
    assert boot2["ci95_bp"][0] is not None


def test_wilson_interval_bounds():
    lo, hi = s4.wilson_interval(8, 10)
    assert lo is not None and hi is not None
    assert 0.0 < lo < 0.8 < hi < 1.0
    assert s4.wilson_interval(0, 0) == (None, None)


def test_short_side_delta_direction(tmp_path):
    # Short: entry 100, real exit 99 (+100bp), virtual exit 98 (+200bp).
    book = _book_with_close(side="short", entry=100.0, exit_px=99.0)
    (tmp_path / ".shadow-book.json").write_text(json.dumps(book))
    cf = [_cf("p1", "cvd_divergence", 98.0)]
    (tmp_path / ".shadow-exit-counterfactual.jsonl").write_text(
        "\n".join(json.dumps(r) for r in cf))
    result = s4.evaluate(tmp_path)
    row = result["arms"]["cvd_divergence"]["rows"][0]
    assert row["real_gross_bp"] == pytest.approx(100.0)
    assert row["virtual_gross_bp"] == pytest.approx(200.0)
    assert row["delta_bp"] == pytest.approx(100.0)


def test_missing_files_are_safe(tmp_path):
    result = s4.evaluate(tmp_path)
    assert result["counterfactual_rows_total"] == 0
    assert all(a["n"] == 0 for a in result["arms"].values())


def _write_pairing_case(tmp_path, book, cfs):
    (tmp_path / ".shadow-book.json").write_text(json.dumps(book))
    (tmp_path / ".shadow-exit-counterfactual.jsonl").write_text(
        "\n".join(json.dumps(r) for r in cfs))
    return s4.evaluate(tmp_path)


def test_pairing_is_scoped_by_account(tmp_path):
    book = {"accounts": {
        "taker": _book_with_close(pid="same-pid", entry=100.0, exit_px=101.0)
        ["accounts"]["taker"],
        "maker": _book_with_close(pid="same-pid", entry=200.0, exit_px=202.0,
                                  account="maker")["accounts"]["maker"],
    }}
    cf = _cf("same-pid", "cvd_divergence", 203.0)
    cf["account"] = "maker"
    result = _write_pairing_case(tmp_path, book, [cf])
    rows = result["arms"]["cvd_divergence"]["rows"]
    assert len(rows) == 1
    assert rows[0]["account"] == "maker"
    assert rows[0]["real_gross_bp"] == pytest.approx(100.0)
    assert rows[0]["delta_bp"] == pytest.approx(50.0)


def test_legacy_pairing_rejects_cross_account_ambiguity(tmp_path):
    book = {"accounts": {
        "taker": _book_with_close(pid="same-pid")["accounts"]["taker"],
        "maker": _book_with_close(pid="same-pid", account="maker")
        ["accounts"]["maker"],
    }}
    cf = _cf("same-pid", "cvd_divergence", 102.0)
    del cf["account"]
    result = _write_pairing_case(tmp_path, book, [cf])
    assert result["ambiguous_virtual_exits"] == 1
    assert result["arms"]["cvd_divergence"]["n"] == 0


def test_duplicate_virtual_rows_are_counted_not_repaired_twice(tmp_path):
    book = _book_with_close("p1", "BTC", "long", 100.0, 101.0)
    cf = _cf("p1", "cvd_divergence", 102.0)
    result = _write_pairing_case(tmp_path, book, [cf, dict(cf)])
    assert result["duplicate_virtual_exits"] == 1
    assert result["arms"]["cvd_divergence"]["n"] == 1


def test_duplicate_real_close_is_quarantined(tmp_path):
    book = _book_with_close("p1", "BTC", "long", 100.0, 101.0)
    book["accounts"]["taker"]["fills"].append(
        {"type": "close", "position_id": "p1", "coin": "BTC", "side": "long",
         "price": 102.0})
    result = _write_pairing_case(
        tmp_path, book, [_cf("p1", "cvd_divergence", 103.0)])
    assert result["ambiguous_virtual_exits"] == 1
    assert result["duplicate_real_close_keys"] == [
        {"account": "taker", "position_id": "p1"}]
    assert result["arms"]["cvd_divergence"]["n"] == 0
