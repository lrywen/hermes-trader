"""Tests for scripts/s4_pullback_scenario_eval.py (S4-3/S4-4).

Pins:
  * reconciliation filter (only win/loss with finite pnl_pct)
  * promotion rule: n>=30 AND win_rate>0.5 AND net@9.26 median>0
  * small sample → inconclusive
  * regime / duration stratification
  * three cost tiers reported
  * missing file safe
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
from datetime import datetime, timezone

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "s4_pullback_scenario_eval.py"
_spec = importlib.util.spec_from_file_location("s4_pullback", _SCRIPT)
s4 = importlib.util.module_from_spec(_spec)
sys.modules["s4_pullback"] = s4
_spec.loader.exec_module(s4)  # type: ignore[union-attr]


def _row(pnl_pct, outcome=None, regime="up", entry_ts=0, exit_ts=None):
    if outcome is None:
        outcome = "win" if pnl_pct > 0 else "loss"
    return {
        "pnl_pct": pnl_pct,
        "outcome": outcome,
        "regime": regime,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts if exit_ts is not None else entry_ts + 5 * 3_600_000,
    }


def test_missing_file_safe(tmp_path):
    result = s4.evaluate(tmp_path / "nope.jsonl")
    assert result["s4_3_pullback_candidate"]["overall"]["n"] == 0
    assert result["s4_3_pullback_candidate"]["decision"] == "inconclusive"


def test_only_reconciled_rows_count(tmp_path):
    rows = [
        _row(1.0),
        _row(-0.5),
        {"outcome": "pending", "pnl_pct": 9.9},       # excluded: pending
        {"outcome": "win", "pnl_pct": None},          # excluded: non-finite pnl
    ]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    assert result["s4_3_pullback_candidate"]["overall"]["n"] == 2
    # Only the "pending" outcome row counts as unreconciled; the non-finite
    # win row fails the reconciled filter and is simply not counted anywhere.
    assert result["pending_unreconciled_rows"] == 1
    assert result["total_rows"] == 4


def test_small_sample_inconclusive(tmp_path):
    rows = [_row(2.0) for _ in range(10)]  # all winners but n < 30
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    assert result["s4_3_pullback_candidate"]["decision"] == "inconclusive"
    assert "n=10" in result["s4_3_pullback_candidate"]["reason"]


def test_promotion_rule_pass(tmp_path):
    # 40 rows, 30 winners (+1% gross) and 10 losers (-0.2%).
    rows = [_row(1.0) for _ in range(30)] + [_row(-0.2) for _ in range(10)]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    c = result["s4_3_pullback_candidate"]
    assert c["overall"]["n"] == 40
    assert c["decision"] == "candidate_promote"
    # gross median: sorted, middle values around +1% → +100bp; net@9.26 > 0.
    assert c["overall"]["net_9_26_median_bp"] > 0


def test_keep_shadow_when_net_negative(tmp_path):
    # 40 rows, majority win but tiny gross so net@9.26 median is negative.
    rows = [_row(0.05) for _ in range(25)] + [_row(-0.05) for _ in range(15)]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    c = result["s4_3_pullback_candidate"]
    assert c["overall"]["n"] == 40
    # gross median is +0.05% = +5bp; net@9.26 = -4.26bp → keep shadow.
    assert c["decision"] == "keep_shadow"


def test_regime_stratification(tmp_path):
    rows = [_row(1.0, regime="up") for _ in range(20)]
    rows += [_row(-1.0, regime="chop") for _ in range(20)]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    by_regime = result["s4_3_pullback_candidate"]["by_btc_regime"]
    assert set(by_regime.keys()) == {"chop", "up"}
    assert by_regime["up"]["n"] == 20
    assert by_regime["chop"]["n"] == 20


def test_duration_buckets(tmp_path):
    # 2h hold → lt_4h; 10h hold → 4h_to_24h.
    rows = [
        _row(1.0, entry_ts=0, exit_ts=2 * 3_600_000),
        _row(1.0, entry_ts=0, exit_ts=10 * 3_600_000),
    ]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    by_dur = result["s4_4_scenario_stratification"]["by_hold_duration"]
    assert by_dur["lt_4h"]["n"] == 1
    assert by_dur["4h_to_24h"]["n"] == 1


def test_unknown_duration_when_timestamps_missing(tmp_path):
    rows = [{"pnl_pct": 1.0, "outcome": "win", "regime": "up"}]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    by_dur = result["s4_4_scenario_stratification"]["by_hold_duration"]
    assert by_dur["unknown"]["n"] == 1


def test_cost_tiers_reported(tmp_path):
    rows = [_row(1.0) for _ in range(5)]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    result = s4.evaluate(f)
    overall = result["s4_3_pullback_candidate"]["overall"]
    assert "gross_median_bp" in overall
    assert "net_3_median_bp" in overall
    assert "net_9_26_median_bp" in overall
    # +1% = +100bp gross.
    assert overall["gross_median_bp"] == pytest.approx(100.0)
    assert overall["net_3_median_bp"] == pytest.approx(97.0)
    assert overall["net_9_26_median_bp"] == pytest.approx(90.74)


def test_fixed_matrix_has_complete_cells_costs_wilson_and_worst_case(tmp_path):
    rows = [_row(1.0, regime="up", entry_ts=0, exit_ts=2 * 3_600_000),
            _row(-0.2, regime="chop", entry_ts=0, exit_ts=10 * 3_600_000),
            _row(0.4, regime="reversal", entry_ts=0, exit_ts=20 * 3_600_000)]
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    fixed = s4.evaluate(f)["s4_4_scenario_stratification"]["fixed_scenarios"]
    assert set(fixed["matrix"]) == {"trend", "chop", "reversal", "unknown"}
    assert all(set(cells) == {"4h", "12h", "24h", "72h"}
               for cells in fixed["matrix"].values())
    assert fixed["matrix"]["unknown"]["4h"]["n"] == 0
    trend = fixed["matrix"]["trend"]["4h"]
    assert trend["gross_median_bp"] == 100.0
    assert trend["net_3_median_bp"] == 97.0
    assert trend["net_9_26_median_bp"] == 90.74
    assert trend["win_rate_wilson95"][0] < trend["win_rate_wilson95"][1]
    assert fixed["worst_case"]["regime"] == "chop"
    assert fixed["worst_case"]["horizon"] == "12h"


def test_reconciled_fee_net_input_is_not_charged_twice(tmp_path):
    row = _row(0.07)
    row["fee_pct"] = 0.03
    f = tmp_path / "pb.jsonl"
    f.write_text(json.dumps(row))
    candidate = s4.evaluate(f)["s4_3_pullback_candidate"]
    assert candidate["overall"]["gross_median_bp"] == pytest.approx(10.0)
    assert candidate["overall"]["net_3_median_bp"] == pytest.approx(7.0)
    assert candidate["overall"]["net_9_26_median_bp"] == pytest.approx(0.74)
    assert candidate["input_cost_basis_counts"] == {"derived_from_fee_net": 1}


def test_walk_forward_12_4_out_of_sample_acceptance(tmp_path):
    rows = []
    for month in range(16):
        year = 2025 + month // 12
        month_no = month % 12 + 1
        exit_ts = int(datetime(year, month_no, 15,
                               tzinfo=timezone.utc).timestamp() * 1000)
        for _ in range(8):
            rows.append(_row(0.2, entry_ts=exit_ts - 3_600_000, exit_ts=exit_ts))
    f = tmp_path / "pb.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows))
    oos = s4.evaluate(f)["s4_4_scenario_stratification"]["out_of_sample_acceptance"]
    assert oos["decision"] == "pass"
    assert len(oos["folds"]) == 1
    assert oos["folds"][0]["train"]["n"] == 96
    assert oos["folds"][0]["test"]["n"] == 32
    assert oos["folds"][0]["test_worst_case"]["net_9_26_mean_bp"] > 0
