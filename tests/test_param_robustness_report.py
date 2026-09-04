"""Unit tests for scripts/param_robustness_report.py (roadmap §4).

Loads pf_dual_period_report first (sys.modules registered) then the
robustness report, mirroring the S2 census test loader pattern.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pf = _load("pf_dual_period_report", "pf_dual_period_report.py")
rb = _load("param_robustness_report", "param_robustness_report.py")


# ── grid generation ──────────────────────────────────────────────────────────

def test_build_grid_symmetry_and_baseline_index():
    vals, idx = rb.build_grid(4.0, 0.20, 4)
    assert len(vals) == 9          # 4 steps/side + baseline
    assert vals[0] == pytest.approx(3.2)
    assert vals[-1] == pytest.approx(4.8)
    assert vals[idx] == pytest.approx(4.0)
    # strictly ascending, equally spaced
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    assert all(d == pytest.approx(diffs[0]) for d in diffs)


def test_build_grid_ascending():
    vals, _ = rb.build_grid(5.0, 0.20, 2)
    assert vals == sorted(vals)


def test_build_grid_integer_dedup():
    # lookback=2 +/-20% => [1.6..2.4] rounds to {2}: degenerate single point.
    vals, idx = rb.build_grid(2.0, 0.20, 4, is_int=True)
    assert vals == [2.0]
    assert idx == 0


def test_build_grid_integer_spread():
    # 72 +/-20% => 57.6..86.4 rounds to multiple unique ints, baseline present.
    vals, idx = rb.build_grid(72.0, 0.20, 4, is_int=True)
    assert len(vals) > 1
    assert vals[idx] == pytest.approx(72.0)


# ── segment splitting ────────────────────────────────────────────────────────

def test_split_mid_ts_is_midpoint():
    assert rb.split_mid_ts(1000, 2000) == 1500
    assert rb.split_mid_ts(0, 100) == 50


def _trade(ts: int, net: float, gross: float = None) -> "pf.Trade":
    return pf.Trade(source="s", side="long", coin="BTC", entry_ts=ts,
                    gross_pct=net if gross is None else gross, net_pct=net)


def test_segment_pfs_split_at_mid():
    trades = [_trade(10, 1.0), _trade(11, -1.0),   # first half (<15)
              _trade(20, 2.0), _trade(30, -1.0)]   # second half (>=15)
    p1, p2, n1, n2 = rb.segment_pfs(trades, 15, use_net=True)
    assert (n1, n2) == (2, 2)
    assert p1 == pytest.approx(1.0)   # +1 / |-1|
    assert p2 == pytest.approx(2.0)   # +2 / |-1|


def test_segment_pfs_no_losses_returns_none():
    trades = [_trade(10, 1.0), _trade(20, 2.0)]
    p1, p2, n1, n2 = rb.segment_pfs(trades, 15, use_net=True)
    assert p1 is None and n1 == 1
    assert p2 is None and n2 == 1


def test_segment_pfs_uses_gross_when_requested():
    trades = [_trade(10, net=0.5, gross=1.0), _trade(11, net=-0.5, gross=-1.0),
              _trade(20, net=0.5, gross=1.0), _trade(30, net=-0.5, gross=-1.0)]
    p1, p2, _, _ = rb.segment_pfs(trades, 15, use_net=False)
    assert p1 == pytest.approx(1.0)
    assert p2 == pytest.approx(1.0)


# ── half gate ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("p1,p2,n1,n2,expected", [
    (1.2, 1.1, 20, 20, "PASS"),
    (None, 1.1, 20, 20, "PASS"),    # no losses in H1 clears
    (0.9, 1.2, 20, 20, "FAIL"),
    (1.2, 0.9, 20, 20, "FAIL"),
    (1.5, 1.5, 10, 20, "INSUFFICIENT"),
    (1.5, 1.5, 20, 5, "INSUFFICIENT"),
])
def test_half_gate(p1, p2, n1, n2, expected):
    assert rb.half_gate(p1, p2, n1, n2, 15, 1.0) == expected


# ── isolated spike ───────────────────────────────────────────────────────────

def test_is_isolated_spike_true():
    # baseline idx2 = 2.0, neighbours 1.0 (< 1.5 threshold) => spike
    vals = [1.0, 1.0, 2.0, 1.0, 1.0]
    assert rb.is_isolated_spike(vals, 2, 0.25) is True


def test_is_isolated_spike_false_on_smooth_plateau():
    vals = [1.1, 1.15, 1.2, 1.15, 1.1]
    assert rb.is_isolated_spike(vals, 2, 0.25) is False


def test_is_isolated_spike_endpoints_never_spike():
    vals = [2.0, 1.0, 1.0, 1.0, 1.0]
    assert rb.is_isolated_spike(vals, 0, 0.25) is False
    assert rb.is_isolated_spike(vals, 4, 0.25) is False


def test_is_isolated_spike_none_peak_or_neighbour():
    # None peak (no losses) cannot be a spike.
    assert rb.is_isolated_spike([1.0, 1.0, None, 1.0, 1.0], 2, 0.25) is False
    # A None neighbour (no losses) cannot undercut the peak.
    assert rb.is_isolated_spike([1.0, None, 2.0, 1.0, 1.0], 2, 0.25) is False


def test_is_isolated_spike_rel_tol_band():
    # neighbour at 1.6 vs peak 2.0 => threshold 1.5; 1.6 >= 1.5 => not a spike
    vals = [1.6, 1.6, 2.0, 1.6, 1.6]
    assert rb.is_isolated_spike(vals, 2, 0.25) is False


# ── adjacent jump ────────────────────────────────────────────────────────────

def test_max_adjacent_jump():
    assert rb.max_adjacent_jump([1.0, 1.1, 1.5, 1.2]) == pytest.approx(0.4)


def test_max_adjacent_jump_flat():
    assert rb.max_adjacent_jump([1.2, 1.2, 1.2]) == pytest.approx(0.0)


def test_max_adjacent_jump_none_breaks_pair():
    # None segments break adjacency; only finite pair is (1.0,1.1)
    assert rb.max_adjacent_jump([1.0, 1.1, None, 3.0]) == pytest.approx(0.1)


# ── aggregate verdict ────────────────────────────────────────────────────────

def _gp(value, full, n=40, h1=1.2, h2=1.2, h1n=20, h2n=20, base=False):
    return rb.GridPoint(value=value, is_baseline=base, full_pf=full, n=n,
                        h1_pf=h1, h2_pf=h2, h1_n=h1n, h2_n=h2n)


def _smooth_grid():
    # 5-point grid, baseline centre (idx2), smoothly varying ~1.2
    return [_gp(3.2, 1.1), _gp(3.6, 1.15),
            _gp(4.0, 1.2, base=True),
            _gp(4.4, 1.15), _gp(4.8, 1.1)]


def test_target_verdict_pass_on_smooth():
    grid = _smooth_grid()
    verdict, reasons = rb.target_verdict(grid, 2, 15, 1.0, 0.25, 0.30)
    assert verdict == "PASS"
    assert reasons == []


def test_target_verdict_insufficient():
    grid = _smooth_grid()
    grid[2] = _gp(4.0, 1.2, n=20, h1=1.2, h2=1.2, h1n=10, h2n=10, base=True)
    verdict, _ = rb.target_verdict(grid, 2, 15, 1.0, 0.25, 0.30)
    assert verdict == "INSUFFICIENT"


def test_target_verdict_fail_on_weak_segment():
    grid = _smooth_grid()
    grid[2] = _gp(4.0, 1.2, h1=0.8, h2=1.2, base=True)
    verdict, reasons = rb.target_verdict(grid, 2, 15, 1.0, 0.25, 0.30)
    assert verdict == "FAIL"
    assert any("half-year" in r for r in reasons)


def test_target_verdict_fail_on_spike():
    grid = _smooth_grid()
    # neighbours drop to 0.9 while baseline is a lone 2.0 peak
    grid[1] = _gp(3.6, 0.9, h1=1.2, h2=1.2)
    grid[3] = _gp(4.4, 0.9, h1=1.2, h2=1.2)
    grid[2] = _gp(4.0, 2.0, h1=1.2, h2=1.2, base=True)
    verdict, reasons = rb.target_verdict(grid, 2, 15, 1.0, 0.25, 0.30)
    assert verdict == "FAIL"
    assert any("spike" in r for r in reasons)


def test_target_verdict_fail_on_large_jump():
    # baseline segments fine, no isolated spike (alternating), but jumps huge
    grid = [_gp(3.2, 1.0, h1=1.2, h2=1.2),
            _gp(3.6, 1.9, h1=1.2, h2=1.2),
            _gp(4.0, 1.0, h1=1.2, h2=1.2, base=True),
            _gp(4.4, 1.9, h1=1.2, h2=1.2),
            _gp(4.8, 1.0, h1=1.2, h2=1.2)]
    verdict, reasons = rb.target_verdict(grid, 2, 15, 1.0, 0.25, 0.30)
    assert verdict == "FAIL"
    assert any("smooth" in r for r in reasons)


# ── sweep wiring: threshold actually gates firings ───────────────────────────

def test_sweep_target_threshold_gates_firings():
    # Flat warm-up, then a sharp +6%/2-bar burst, then a flat tail so the
    # burst bar still has hold_bars of forward room to be scored.
    candles = []
    base_t = 1_000_000
    px = 100.0

    def _bar(i, close):
        candles.append(pf.Candle(t=base_t + i * 300_000, o=px, h=max(px, close),
                                 l=min(px, close), c=close, v=10.0))

    for i in range(11):          # index 0..10 flat at 100
        _bar(i, px)
    for up in (3.0, 3.0):        # index 11,12: +3% then +3% => ~+6% over 2 bars
        npx = px * (1 + up / 100.0)
        _bar(len(candles), npx)
        px = npx
    for _ in range(5):           # index 13..17 flat tail at the high
        _bar(len(candles), px)

    target = rb.SCAN_TARGETS[0]  # momentum_burst pct_threshold baseline 4.0
    # grid straddling the ~6% move: 4.0 fires, 8.0 does not
    grid_values = [4.0, 8.0]
    mid = candles[0].t + 1
    points = rb._sweep_target(target, grid_values, 0, {"BTC": candles},
                              {"BTC": 0.0}, hold_bars=2, mid_ts=mid,
                              use_net=False, warmup=10)
    assert points[0].n >= 1     # threshold 4% -> burst fires
    assert points[1].n == 0     # threshold 8% -> move too small, no fire


def test_scan_targets_baselines_present():
    for t in rb.SCAN_TARGETS:
        vals, idx = rb.build_grid(t.baseline, 0.20, 4)
        assert vals[idx] == pytest.approx(t.baseline)
