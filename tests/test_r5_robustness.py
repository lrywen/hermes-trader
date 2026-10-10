"""R5 tests: WFA + DSR/PBO wired into the shadow-grade rating card."""
from __future__ import annotations

import pytest

from scripts import shadow_grade as sg
from hermes_trader.validation import walk_forward_analysis

DAY_MS = 86_400_000


def _records(days, pnl_pct, *, start_ms=DAY_MS * 20000):
    """One graded record per UTC day carrying pnl_pct."""
    out = []
    for i in range(days):
        out.append({"ts": start_ms + i * DAY_MS, "pnl_pct": pnl_pct})
    return out


def test_daily_bps_aggregation():
    recs = _records(3, 0.1)  # 0.1 percent = 10 bps
    series = sg._r5_daily_bps(recs)
    assert len(series) == 3
    assert series == [10.0, 10.0, 10.0]


def test_ungraded_records_excluded():
    recs = [{"ts": DAY_MS * 20000}, {"ts": DAY_MS * 20001, "pnl_pct": 0.1}]
    series = sg._r5_daily_bps(recs)
    assert len(series) == 1


def test_r5_none_when_insufficient_days():
    assert sg.r5_robustness(_records(10, 0.1)) is None


def test_r5_block_for_consistent_positive_edge():
    # 0.1% = 10 bps positive every day for 24 days -> positive block.
    block = sg.r5_robustness(_records(24, 0.1))
    assert block is not None
    assert block["net_edge_bps"] > 0
    assert block["bb_ci_lo_bps"] > 0
    assert block["wfr"] > 0.5
    assert block["dsr_passes"] in (True, False)
    assert block["pbo"] == 0.0


def test_r5_block_for_consistent_negative_edge():
    block = sg.r5_robustness(_records(24, -0.1))
    assert block["net_edge_bps"] < 0
    assert block["wfr"] == 0.0
    assert block["pbo"] > 0


def test_wfa_window_count_and_shapes():
    series = [1.0] * 24
    # train 12, test 4 -> windows at start 0,4,8 -> 3 windows (8+12+4=24 fits)
    res = walk_forward_analysis(series, train_size=12, test_size=4)
    # constant series has stdev 0 -> sharpe 0; windows still formed
    assert res.n_windows == 3
    assert len(res.oos_sharpe) == 3


def test_wfa_raises_on_short_series():
    with pytest.raises(ValueError):
        walk_forward_analysis([1.0] * 5, train_size=12, test_size=4)


def test_grade_arm_attaches_r5_stats():
    recs = _records(24, 0.1)
    out = sg.grade_arm(
        "signal_age_decay", "shadow", "/nonexistent.jsonl",
        windows=(24, 72), records=recs)
    assert "r5_stats" in out
    assert out["independent_days"] == 24
    assert out["realistic_fill_positive"] is True
