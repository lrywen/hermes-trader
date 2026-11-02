"""P0-2 slippage measurement: pure logic — tiers, direction sign, reporting."""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the script as a module (scripts/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "measure_realized_slippage",
    Path(__file__).resolve().parents[1] / "scripts" / "measure_realized_slippage.py")
mrs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrs)


def test_tier_boundaries():
    assert mrs._tier(0) == "micro_lt5M"
    assert mrs._tier(4.9e6) == "micro_lt5M"
    assert mrs._tier(5e6) == "small_5M_50M"
    assert mrs._tier(49e6) == "small_5M_50M"
    assert mrs._tier(50e6) == "medium_50M_200M"
    assert mrs._tier(200e6) == "large_ge200M"
    assert mrs._tier(None) == "micro_lt5M"  # missing volume treated as thinnest


def _pair(side, adv, tier="small_5M_50M"):
    return {"coin": "X", "side": side, "tier": tier, "adverse_bp": adv}


def test_report_partitions_and_contrast():
    pairs = [
        _pair("long", 1.0), _pair("short", -1.0), _pair("long", 3.0),   # small
        _pair("long", 2.0, "medium_50M_200M"),                            # >=50M
    ]
    rep = mrs.report(pairs)
    assert rep["total_pairs"] == 4
    assert rep["by_tier"]["small_5M_50M"]["n"] == 3
    assert rep["by_tier"]["medium_50M_200M"]["n"] == 1
    assert rep["small_vs_large"]["small_5_50M"]["n"] == 3
    assert rep["small_vs_large"]["medium_or_large_ge50M"]["n"] == 1
    assert rep["by_side"]["long"]["n"] == 3 and rep["by_side"]["short"]["n"] == 1


def test_quantiles_and_frac_adverse():
    pairs = [_pair("long", float(x)) for x in range(-2, 9)]  # -2..8, 8 of 11 adverse (>0)
    blk = mrs.report(pairs)["by_tier"]["small_5M_50M"]
    assert blk["n"] == 11
    assert blk["frac_adverse_pct"] == round(100 * 8 / 11, 1)
    assert blk["median_bp"] == 3.0


def test_bootstrap_ci_contains_mean_for_symmetric_sample():
    # tight symmetric sample: mean CI should straddle the sample mean
    xs = [-1.0, 1.0] * 50
    lo, hi = mrs._boot_mean_ci(xs, B=500, seed=1)
    assert lo <= 0.0 <= hi
