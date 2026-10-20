"""Tests for scripts/bf_bucket_forward_returns.py.

Cover the pure, network-free core: point-in-time entry alignment (no forming
bar), forward-return math, short-convention flip, and bucket aggregation.
The HL candle fetch path is exercised only inside the container, never here.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(__file__)
_SCRIPT = os.path.join(_HERE, "..", "scripts", "bf_bucket_forward_returns.py")

spec = importlib.util.spec_from_file_location("bf_bucket", _SCRIPT)
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

STEP = bf.STEP  # 15m in ms


def _grid(start_ms, n):
    """Ascending bar-open timestamps aligned to the 15m grid."""
    return [start_ms + i * STEP for i in range(n)]


def test_entry_uses_only_closed_bar_and_forward_returns():
    # closes rise by 1.0 each bar starting at 100
    t0 = 1_000 * STEP
    ts = _grid(t0, 400)
    cl = [100.0 + i for i in range(400)]
    # signal one ms into a bar whose predecessor closed: entry = prior close
    sig = t0 + 10 * STEP + 1
    rows = bf.price_rows(
        [{"coin": "X", "ts": sig, "bf": 3, "verdict": "SHORT",
          "confidence": 0.7, "composite": 40}],
        ts_by_coin={"X": ts}, cl_by_coin={"X": cl})
    assert len(rows) == 1
    r = rows[0]
    # bar opening at t0+10*STEP is still forming; last closed opens t0+9*STEP
    assert r["entry_idx"] == 9
    entry = cl[9]
    assert r["rl_4h"] == pytest.approx(cl[9 + 16] / entry - 1)
    assert r["rl_24h"] == pytest.approx(cl[9 + 96] / entry - 1)


def test_forming_bar_is_never_used_exactly_at_grid():
    t0 = 2_000 * STEP
    ts = _grid(t0, 50)
    cl = [10.0] * 25 + [20.0] * 25
    # signal exactly at a grid open: that bar is NOT closed yet
    sig = t0 + 25 * STEP
    rows = bf.price_rows(
        [{"coin": "X", "ts": sig, "bf": 1}],
        ts_by_coin={"X": ts}, cl_by_coin={"X": cl})
    assert rows[0]["entry_idx"] == 24
    assert rows[0]["rl_4h"] == pytest.approx(20.0 / 10.0 - 1)  # forward is up


def test_future_beyond_series_is_none():
    t0 = 3_000 * STEP
    ts = _grid(t0, 20)
    cl = [float(i) for i in range(20)]
    rows = bf.price_rows(
        [{"coin": "X", "ts": t0 + 2 * STEP + 5, "bf": 0}],
        ts_by_coin={"X": ts}, cl_by_coin={"X": cl})
    r = rows[0]
    assert r["rl_4h"] is not None          # entry idx2 + 16 = 18 < 20
    assert r["rl_48h"] is None             # 2 + 192 beyond series


def test_flip_short_negates_level_and_swaps_ci_and_win():
    a = {"n": 10, "median_pct": 2.0, "mean_pct": 3.0,
         "ci_lo_pct": 1.0, "ci_hi_pct": 4.0, "win_pct": 70.0}
    s = bf._flip_short(a)
    assert s["median_pct"] == -2.0
    assert s["mean_pct"] == -3.0
    assert (s["ci_lo_pct"], s["ci_hi_pct"]) == (-4.0, -1.0)
    assert s["win_pct"] == 30.0
    # passthrough for under-sized bucket
    assert bf._flip_short({"n": 2}) == {"n": 2}


def test_summarize_buckets_and_verdict_tables():
    t0 = 4_000 * STEP
    ts = _grid(t0, 300)
    # strong-bullish (bf 3/4) SHORT setups drift up, LONG drift down
    sigs = []
    for i in range(6):
        sigs.append({"coin": f"S{i}", "ts": t0 + STEP + 1, "bf": 3,
                     "verdict": "SHORT"})
    for i in range(6):
        sigs.append({"coin": f"L{i}", "ts": t0 + STEP + 1, "bf": 4,
                     "verdict": "LONG"})
    cl = {c: [100.0 + k for k in range(300)] for c in
          {s["coin"] for s in sigs}}
    # make LONG coins decline instead
    for i in range(6):
        cl[f"L{i}"] = [100.0 - k for k in range(300)]
    ts_map = {c: ts for c in cl}
    rows = bf.price_rows(sigs, ts_by_coin=ts_map, cl_by_coin=cl)
    res = bf.summarize(rows, n_signals=len(sigs), n_coins=len(cl),
                       fetch_fail=[])
    assert res["n_rows_priced"] == len(rows)
    s = res["strong_bull_verdict_long"]["SHORT"]["4h"]
    l = res["strong_bull_verdict_long"]["LONG"]["4h"]
    assert s["median_pct"] > 0 and l["median_pct"] < 0
    # short-convention bucket is the exact negation of long at same bf
    assert res["by_bf_short"]["3"]["4h"]["median_pct"] == \
        pytest.approx(-res["by_bf_long"]["3"]["4h"]["median_pct"])
