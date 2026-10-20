"""Tests for the surface-channel forward-return analysis (L1 audit F2).

Covers channel classification precedence (mutually exclusive, mirrors the
perception OR gate), channel/band preservation through PIT pricing,
cross-sectional de-meaning, and the coin-clustered contrast (clustering is on
coin, so repeated signals on one coin must not inflate the sample).
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_HERE, "..", "scripts", fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bf = _load("bf_bucket", "bf_bucket_forward_returns.py")
ch = _load("ch_bucket", "ch_bucket_forward_returns.py")
ext = _load("ch_extract", "ch_signal_extract.py")

STEP = bf.STEP


def test_classify_mutually_exclusive_precedence():
    # burst beats trend beats pattern beats mover beats gate
    assert ext.classify({"momentumBurst", "uptrendMomentum", "dailyMover"}, 10) == "burst"
    assert ext.classify({"uptrendMomentum", "dailyMover"}, 10) == "trend"
    assert ext.classify({"downtrendMomentum"}, 5) == "trend"
    assert ext.classify({"bullishReversalCandle", "dailyMover"}, 5) == "pattern"
    assert ext.classify({"dailyMover"}, 10) == "mover"
    # genuine gate: no bypass but score >= 54
    assert ext.classify({"trendStrength", "higherLows1h"}, 60) == "gate"
    # sub-threshold with no bypass never surfaces (classified other)
    assert ext.classify({"trendStrength"}, 20) == "other"


def test_bands():
    assert ext.composite_band(60) == ">=54"
    assert ext.composite_band(50) == "45-54"
    assert ext.composite_band(40) == "35-45"
    assert ext.composite_band(30) == "25-35"
    assert ext.composite_band(10) == "<25"
    assert ext.composite_band(None) == "na"


def _mk_signals():
    t0 = 5_000 * STEP
    return ([{"coin": "TREND", "ts": t0 + STEP + 1, "composite": 22,
              "channel": "trend", "band": "<25", "fired": ["uptrendMomentum"],
              "verdict": None, "conf": None}],
            [{"coin": "GATE", "ts": t0 + STEP + 1, "composite": 58,
              "channel": "gate", "band": ">=54", "fired": ["trendStrength"],
              "verdict": None, "conf": None}]), t0


def test_price_signals_preserves_channel_and_pit():
    (trend, gate), t0 = _mk_signals()
    sigs = trend + gate
    ts = [t0 + i * STEP for i in range(300)]
    cl = {"TREND": [100.0 + i for i in range(300)],
          "GATE": [100.0 - i for i in range(300)]}
    rows = ch.price_signals(sigs, ts_by_coin={c: ts for c in cl}, cl_by_coin=cl)
    assert len(rows) == 2
    by = {r["coin"]: r for r in rows}
    assert by["TREND"]["channel"] == "trend"
    assert by["GATE"]["channel"] == "gate"
    assert by["TREND"]["entry_idx"] == 0  # signal 1ms into bar idx1 -> last closed is idx0


def test_demean_removes_shared_cross_section():
    # two coins in the SAME time bucket, identical +10% at horizon -> dm = 0
    rows = [
        {"ts": 1 * STEP + 1, "channel": "trend", "coin": "A", "rl_24h": 0.10},
        {"ts": 1 * STEP + 2, "channel": "gate", "coin": "B", "rl_24h": 0.10},
    ]
    ch.add_demeaned(rows)
    assert rows[0]["dm_24h"] == pytest.approx(0.0)
    assert rows[1]["dm_24h"] == pytest.approx(0.0)
    # differing returns de-mean around the group mean
    rows[0]["rl_24h"] = 0.20
    rows[1]["rl_24h"] = 0.00
    ch.add_demeaned(rows)
    assert rows[0]["dm_24h"] == pytest.approx(0.10)
    assert rows[1]["dm_24h"] == pytest.approx(-0.10)


def test_coin_clustered_contrast_detects_separation_and_clusters():
    # 8 coins observed in both groups; trend consistently +3% vs gate flat.
    # Each coin has 20 repeated trend signals but must count as ONE coin.
    rows = []
    for ci in range(8):
        coin = f"C{ci}"
        for _ in range(20):
            rows.append({"coin": coin, "channel": "trend",
                         "rl_24h": 0.03, "dm_24h": 0.03, "ts": 1})
        rows.append({"coin": coin, "channel": "gate",
                     "rl_24h": 0.00, "dm_24h": 0.00, "ts": 1})
    res = ch.coin_cluster_contrast(rows, "trend", "gate", "24h", "rl_",
                                   n_boot=1000)
    assert res["n_coins"] == 8            # clustered, not 21*8
    assert res["point_diff_pct"] == pytest.approx(3.0)
    assert res["ci_lo_pct"] > 0           # significant positive


def test_agg_exact_stats_large_sample_ci_capped():
    # exact median/win/mean are computed over ALL values even when the CI
    # sub-sample is capped; CI keys present and bracket the median.
    vals = [(-0.02 if i % 3 else 0.01) for i in range(10_000)]
    a = ch.agg_h_fast(vals, label_seed=1)
    assert a["n"] == 10_000
    assert a["mean_pct"] == round(100 * sum(vals) / len(vals), 3)
    assert a["ci_lo_pct"] <= a["median_pct"] <= a["ci_hi_pct"]


def test_coin_clustered_contrast_insufficient_when_few_coins():
    rows = [{"coin": "A", "channel": "trend", "rl_4h": 0.01, "ts": 1},
            {"coin": "A", "channel": "gate", "rl_4h": 0.0, "ts": 1}]
    res = ch.coin_cluster_contrast(rows, "trend", "gate", "4h", "rl_")
    assert res.get("insufficient") is True
