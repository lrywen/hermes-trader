"""P0-2 slippage measurement: tiers, direction sign, anti-look-ahead, day CI."""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the script as a module (scripts/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "measure_realized_slippage",
    Path(__file__).resolve().parents[1] / "scripts" / "measure_realized_slippage.py")
mrs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrs)

BAR = mrs.BAR_MS
# fixed epoch for a 5m bar open
T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR)


def test_tier_boundaries():
    assert mrs._tier(1.0) == "micro_lt5M"
    assert mrs._tier(4.9e6) == "micro_lt5M"
    assert mrs._tier(5e6) == "small_5M_50M"
    assert mrs._tier(49e6) == "small_5M_50M"
    assert mrs._tier(50e6) == "medium_50M_200M"
    assert mrs._tier(200e6) == "large_ge200M"


def test_missing_volume_is_unknown_not_micro():
    # Review D2: ~52% of candidates lack volume; they must NOT be folded into
    # the thinnest real-liquidity bucket.
    assert mrs._tier(None) == "unknown_volume"
    assert mrs._tier(0) == "unknown_volume"
    assert mrs._tier(-5) == "unknown_volume"
    assert "unknown_volume" in mrs.TIER_NAMES


def _pair(side, adv, tier="small_5M_50M", ts=T0):
    return {"coin": "X", "side": side, "tier": tier, "adverse_bp": adv, "ts": ts}


def test_report_partitions_and_contrast():
    pairs = [
        _pair("long", 1.0), _pair("short", -1.0), _pair("long", 3.0),   # small
        _pair("long", 2.0, "medium_50M_200M"),                            # >=50M
        _pair("long", 5.0, "unknown_volume"),                             # missing vol
    ]
    rep = mrs.report(pairs)
    assert rep["total_pairs"] == 5
    assert rep["by_tier"]["small_5M_50M"]["n"] == 3
    assert rep["by_tier"]["medium_50M_200M"]["n"] == 1
    assert rep["by_tier"]["unknown_volume"]["n"] == 1
    # the pivotal contrast uses real-volume tiers only
    assert rep["small_vs_large"]["small_5_50M"]["n"] == 3
    assert rep["small_vs_large"]["medium_or_large_ge50M"]["n"] == 1
    assert rep["by_side"]["long"]["n"] == 4 and rep["by_side"]["short"]["n"] == 1


def test_quantiles_and_frac_adverse():
    pairs = [_pair("long", float(x)) for x in range(-2, 9)]  # -2..8, 8 of 11 >0
    blk = mrs.report(pairs)["by_tier"]["small_5M_50M"]
    assert blk["n"] == 11
    assert blk["frac_adverse_pct"] == round(100 * 8 / 11, 1)
    assert blk["median_bp"] == 3.0


def test_bootstrap_ci_contains_mean_for_symmetric_sample():
    xs = [-1.0, 1.0] * 50
    lo, hi = mrs._boot_mean_ci(xs, B=500, seed=1)
    assert lo <= 0.0 <= hi


# ── D5: anti-look-ahead pairing ────────────────────────────────────────────

def test_pair_uses_signal_close_and_next_bar_open():
    # bar opens at T0, T0+BAR, T0+2BAR; ts anywhere inside the first bar must
    # fill at the SECOND bar's open, never the signal bar's open.
    bars_t = [T0, T0 + BAR, T0 + 2 * BAR]
    bars_c = [100.0, 110.0, 120.0]
    bars_o = [99.0, 101.0, 111.0]
    # decision near the END of bar 0 -> close 100, fill at bar1 open 101
    bar_ts, decision, fill = mrs._pair_bar(T0 + BAR - 1, bars_t, bars_c, bars_o)
    assert bar_ts == T0 and decision == 100.0 and fill == 101.0
    # decision near bar OPEN also pairs within the same bar, fill = next open
    bar_ts2, d2, f2 = mrs._pair_bar(T0 + 1000, bars_t, bars_c, bars_o)
    assert d2 == 100.0 and f2 == 101.0


def test_pair_long_short_direction_signs():
    bars_t = [T0, T0 + BAR]
    bars_c = [100.0, 110.0]
    bars_o = [99.0, 101.0]  # next open +1% -> adverse for long, favourable short
    _, decision, fill = mrs._pair_bar(T0 + 1000, bars_t, bars_c, bars_o)
    raw = (fill - decision) / decision * 1e4
    assert abs(raw - 100.0) < 1e-6  # +100bp; sign flip happens in measure()


def test_pair_rejects_no_next_bar_and_misalignment():
    # ts in the LAST bar -> no next bar -> None
    assert mrs._pair_bar(T0 + 1000, [T0], [100.0], [99.0]) is None
    # ts far from any bar open (gap > one BAR) -> misaligned -> None
    assert mrs._pair_bar(T0 + 5 * BAR, [T0, T0 + BAR], [100.0, 101.0],
                         [99.0, 100.5]) is None
    # ts before first bar -> None
    assert mrs._pair_bar(T0 - BAR, [T0, T0 + BAR], [100.0, 101.0],
                         [99.0, 100.5]) is None
    # non-positive price -> None
    assert mrs._pair_bar(T0 + 1000, [T0, T0 + BAR], [0.0, 101.0],
                         [99.0, 100.5]) is None


# ── D3: UTC day-block bootstrap ────────────────────────────────────────────

def test_day_ci_groups_within_day_and_counts_days():
    # 10 samples on day A, 2 on day B -> 2 independent days, not 12 iid draws
    dayA = [_pair("long", 2.0, ts=T0 + i * 1000) for i in range(10)]
    dayB_ts = T0 + 26 * 3600_000  # next UTC day
    dayB = [_pair("long", -4.0, ts=dayB_ts), _pair("long", -2.0, ts=dayB_ts + 1000)]
    out = mrs._boot_day_ci(dayA + dayB, B=500, seed=7)
    assert out["days"] == 2
    # day means are +2 and -3 -> cross-day mean -0.5
    assert out["day_mean_bp"] == -0.5
    assert out["day_ci95_bp"][0] <= -0.5 <= out["day_ci95_bp"][1]


def test_day_ci_single_day_returns_nan_interval():
    pairs = [_pair("long", 1.0, ts=T0 + i * 1000) for i in range(5)]
    out = mrs._boot_day_ci(pairs, B=100)
    assert out["days"] == 1 and out["day_mean_bp"] == 1.0
    assert out["day_ci95_bp"][0] != out["day_ci95_bp"][0]  # NaN self-check


def test_report_blocks_carry_day_ci():
    blk = mrs.report([_pair("long", 1.0)])["by_side"]["long"]
    assert "day_ci95_bp" in blk and "days" in blk


# ── D1: HyperLiquid same-venue source ──────────────────────────────────────

def test_hl_series_reads_local_perp_cache(tmp_path):
    import json as _json
    cache = {
        "BTC|5m": {
            str(T0): [99.0, 100.5, 98.5, 100.0, 10.0],   # o,h,l,c,v
            str(T0 + BAR): [101.0, 102.0, 100.5, 101.0, 12.0],
        }
    }
    p = tmp_path / "ch.json"
    p.write_text(_json.dumps(cache))
    mrs.hl_cache_load(str(p))
    ser = mrs._series_hl("BTC", T0, T0 + BAR, use_network=False)
    assert ser is not None
    bars_t, bars_c, bars_o = ser
    assert bars_t == [T0, T0 + BAR]
    assert bars_c == [100.0, 101.0]   # close = vector[3]
    assert bars_o == [99.0, 101.0]    # open = vector[0]
    # end-to-end same-venue pair: decision 100 -> next open 101
    bar_ts, d, f = mrs._pair_bar(T0 + 1000, bars_t, bars_c, bars_o)
    assert (d, f) == (100.0, 101.0)


def test_hl_series_missing_coin_returns_none(tmp_path):
    p = tmp_path / "ch.json"
    p.write_text("{}")
    mrs.hl_cache_load(str(p))
    assert mrs._series_hl("NOPE", T0, T0 + BAR, use_network=False) is None
    mrs.hl_cache_load(str(tmp_path / "absent.json"))
    assert mrs._series_hl("NOPE", T0, T0 + BAR, use_network=False) is None
