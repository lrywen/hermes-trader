"""Tests for scripts/outcome_baseline.py (problem-list #1/#2/#6 baseline).

Covers the pure report helpers: stop-cap parsing, reason grouping, group
stats, the slippage/gap-through analysis (the negative-spot_pct behaviour that
was buggy in the first pass), turnover windowing, concurrency timeline
reconstruction, and the signed net-exposure series.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Load the script as a module without executing main().
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "outcome_baseline.py"
_spec = importlib.util.spec_from_file_location("outcome_baseline", _SCRIPT)
ob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ob)


def _close(**kw):
    base = {
        "ts": 0, "coin": "BTC", "side": "long", "entry_px": 1.0, "price": 0.99,
        "mid_px": 0.995, "notional_usd": 100.0, "realized_pnl_usd": -1.0,
        "spot_pct": -1.0, "slippage_pct": 0.05, "reason": "stale_flat_timeout",
        "position_id": "p1",
    }
    base.update(kw)
    return base


def _open(**kw):
    base = {"ts": 0, "position_id": "p1", "side": "long",
            "notional_usd": 100.0}
    base.update(kw)
    return base


# --------------------------------------------------------------------- _stop_cap

def test_stop_cap_parses():
    assert ob._stop_cap("max_loss (2.59% spot / 25.9% ROE >= 2.50% spot cap; x") == 2.50
    assert ob._stop_cap("max_loss (7.78% spot / 23.3% ROE >= 3.00% spot cap; x") == 3.00


def test_stop_cap_none_when_absent():
    assert ob._stop_cap("stale_flat_timeout (240min)") is None


# ----------------------------------------------------------------- _reason_group

def test_reason_group_splits():
    assert ob._reason_group("max_loss (1.5% spot)") == "max_loss"
    assert ob._reason_group("floor_breach (2x consec)") == "floor_breach"
    assert ob._reason_group("ignite_timeout (94min)") == "ignite_timeout"


# ----------------------------------------------------------------- group_stats

def test_group_stats_win_rate_and_pf():
    rows = [_close(realized_pnl_usd=2.0), _close(realized_pnl_usd=-1.0),
            _close(realized_pnl_usd=3.0)]
    s = ob.group_stats(rows)
    assert s["n"] == 3
    assert s["win_rate"] == pytest.approx(66.7, abs=0.1)
    assert s["total_pnl_usd"] == 4.0
    assert s["gross_win_usd"] == 5.0
    assert s["gross_loss_usd"] == 1.0
    assert s["profit_factor"] == 5.0
    assert s["worst_loss_usd"] == -1.0


def test_group_stats_empty():
    s = ob.group_stats([])
    assert s["n"] == 0
    assert s["profit_factor"] is None
    assert s["win_rate"] is None


# ------------------------------------------------------------ slippage_analysis

def test_slippage_fixed_component():
    rows = [_close(notional_usd=100.0, slippage_pct=0.05,
                   realized_pnl_usd=-1.0, spot_pct=-1.0)]
    s = ob.slippage_analysis(rows)
    # fixed = 100 * 0.05 / 100 = 0.05
    assert s["fixed_slippage_usd"] == 0.05
    assert s["gap_through_usd"] == 0.0
    assert s["slippage_total_usd"] == 0.05


def test_slippage_gap_through_negative_spot():
    # long loss: spot -7.78%, cap 3.00% -> gap 4.78%
    rows = [_close(notional_usd=100.0, slippage_pct=0.05, spot_pct=-7.78,
                   realized_pnl_usd=-8.0,
                   reason="max_loss (7.78% spot / 23.3% ROE >= 3.00% spot cap)")]
    s = ob.slippage_analysis(rows)
    # fixed = 100*0.05/100 = 0.05 ; gap = 100 * (7.78-3.00)/100 = 4.78
    assert s["gap_through_usd"] == pytest.approx(4.78)
    assert s["gap_through_rows"] == 1
    assert s["slippage_total_usd"] == pytest.approx(4.83)
    # gross loss = -8.0 -> 4.83 / 8 = 60.4%
    assert s["slippage_of_gross_loss_pct"] == pytest.approx(4.83 / 8.0 * 100, abs=0.2)


def test_slippage_no_gap_when_within_cap():
    rows = [_close(notional_usd=100.0, spot_pct=-2.0, realized_pnl_usd=-2.0,
                   reason="max_loss (2.0% spot / 20% ROE >= 3.00% spot cap)")]
    s = ob.slippage_analysis(rows)
    assert s["gap_through_usd"] == 0.0
    assert s["gap_through_rows"] == 0


def test_slippage_missing_slippage_pct_is_pre_era():
    # Pre-2a8ba70 max_loss closes omit slippage_pct (None). Their fixed-slip is
    # unknown (reported via pre_era_closes, not assumed 0), but gap-through
    # overstop is still computed.
    rows = [_close(ts=1_791_000_000_000, notional_usd=100.0, slippage_pct=None,
                   spot_pct=-5.0, realized_pnl_usd=-5.0,
                   reason="max_loss (5.0% spot >= 3.00% spot cap)")]
    s = ob.slippage_analysis(rows)
    assert s["pre_era_closes"] == 1
    assert s["fixed_slippage_usd"] == 0.0
    assert s["gap_through_usd"] == pytest.approx(2.0)


def test_slippage_post_era_missing_slip_is_not_pre_era():
    # A close after the cutover that still lacks slippage_pct is anomalous, not
    # a legitimate pre-era record; it should not be counted in pre_era_closes.
    rows = [_close(ts=ob._SLIP_ERA_MS + 1, notional_usd=100.0, slippage_pct=None,
                   spot_pct=-5.0, realized_pnl_usd=-5.0,
                   reason="max_loss (5.0% spot >= 3.00% spot cap)")]
    s = ob.slippage_analysis(rows)
    assert s["pre_era_closes"] == 0
    assert s["gap_through_usd"] == pytest.approx(2.0)


# ----------------------------------------------------------------- turnover

def test_turnover_empty():
    t = ob.turnover([], 15)
    assert t["n"] == 0 and t["per_day"] == 0.0 and t["per_year"] == 0.0


def test_turnover_single_close_no_window():
    t = ob.turnover([_close(ts=1_000_000_000_000)], None)
    assert t["n"] == 1
    assert t["window_days"] == 0.0  # single point span ~0


def test_turnover_windowed_keeps_trailing():
    # 100 rows over 20 days, window 10 days should keep only trailing half.
    day_ms = 86_400_000
    rows = [_close(ts=1_000_000_000_000 + i * day_ms) for i in range(20)]
    t = ob.turnover(rows, 10)
    # cutoff = last - 10d; rows with ts >= cutoff: the last 11 (i=9..19)
    assert t["n"] == 11
    assert t["window_days"] == 10.0
    assert t["per_day"] == pytest.approx(1.1)


def test_turnover_all_time_uses_full_span():
    day_ms = 86_400_000
    rows = [_close(ts=1_000_000_000_000 + i * day_ms) for i in range(5)]
    t = ob.turnover(rows, None)
    assert t["n"] == 5
    assert t["window_days"] == pytest.approx(4.0, abs=0.1)
    assert t["per_day"] == pytest.approx(1.25, abs=0.05)


# ---------------------------------------------------------------- concurrency

def test_concurrency_reconstructs_overlap():
    opens = [_open(ts=0, position_id="a"), _open(ts=1, position_id="b")]
    closes = [_close(ts=2, position_id="a"), _close(ts=3, position_id="b")]
    c = ob.concurrency(closes, opens)
    assert c["max_open"] == 2
    # timeline: t0 open a -> cur1; t1 open b -> cur2; t2 close a ->1; t3 close b->0
    # intervals: [0,1) cur1, [1,2) cur2, [2,3) cur1 => weighted (1*1+1*2+1*1)/3 = 1.333
    assert c["avg_open"] == pytest.approx(1.333, abs=0.01)


def test_concurrency_empty():
    c = ob.concurrency([], [])
    assert c["max_open"] == 0
    assert c["avg_open"] == 0.0


# ---------------------------------------------------------- net_exposure_series

def test_net_exposure_long_short_netting():
    opens = [_open(ts=0, position_id="long", side="long", notional_usd=200.0),
             _open(ts=1, position_id="short", side="short", notional_usd=100.0)]
    closes = [_close(ts=2, position_id="long"), _close(ts=3, position_id="short")]
    ne = ob.net_exposure_series(closes, opens)
    # samples after each event: +200, +100, -100, 0 -> mean 50, max_abs 200
    assert ne["n_samples"] == 4
    assert ne["mean_net_usd"] == pytest.approx(50.0, abs=0.01)
    assert ne["max_abs_net_usd"] == pytest.approx(200.0, abs=0.01)


def test_net_exposure_empty():
    ne = ob.net_exposure_series([], [])
    assert ne["n_samples"] == 0
    assert ne["mean_net_usd"] == 0.0
