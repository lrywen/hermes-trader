"""Tests for scripts/regen_param_sweep.py — regenerating-signal replayer,
walk-forward parameter sweep, and the property lock between the arithmetic
verdict replays and the source-of-truth gate functions."""

import importlib.util
import json
import math
from pathlib import Path

import pytest

from hermes_trader.agents.ta_filter import late_entry_check, relax_tier_check
from hermes_trader.data import historical_candles as hc
from hermes_trader.data.historical_candles import Candle

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "regen_param_sweep", ROOT / "scripts" / "regen_param_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BAR4 = hc.INTERVAL_MS["4h"]
BAR1H = hc.INTERVAL_MS["1h"]
BAR1D = hc.INTERVAL_MS["1d"]
G0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR4)


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "bars.json"))
    yield
    hc.reset_cache()


def _candles(closes, t0=G0, bar=BAR4):
    return [Candle(t=t0 + k * bar, o=c, h=c * 1.002, l=c * 0.998, c=c, v=1.0)
            for k, c in enumerate(closes)]


def _regime_closes(n=160):
    """flat -> strong rally -> choppy drift -> crash: exercises the strict,
    trend-relaxed and mirror-short branches of the veto."""
    closes, px = [], 100.0
    for k in range(n):
        if k < 60:
            px *= 1.0 + 0.001 * math.sin(k)
        elif k < 120:
            px *= 1.008
        elif k < 140:
            px *= 1.0 - 0.002 * ((k % 4) - 1.5)
        else:
            px *= 0.985
        closes.append(px)
    return closes


def _params(mod, **over):
    p = dict(mod.BASE_PARAMS)
    p["mtf_enabled"] = False  # production setting; replay assumes no 15m
    p.update(over)
    return p


# ---------------------------------------------------------------------------
# Forward grading geometry (mirrors backfill_ta_late_entry_historical)
# ---------------------------------------------------------------------------

def test_forward_grade_geometry():
    mod = _load_module()
    bars = _candles([100.0, 105.0, 106.0, 104.0])
    g = mod.forward_grade(bars, 0, "long", hold_bars=2)
    # B1 = bars[1]; entry = B0.close = 100; exit = close of bar b1-2+2 = bars[1]
    assert g["exit_px"] == 105.0
    assert g["net_pct"] == pytest.approx(4.95)   # +5% gross - 5bps
    assert g["mae_pct"] == pytest.approx(0.0)
    gs = mod.forward_grade(bars, 0, "short", hold_bars=2)
    assert gs["net_pct"] == pytest.approx(-5.05)
    # a dip inside the hold window shows in MAE (gross, no fee)
    bars2 = _candles([100.0, 90.0, 105.0, 104.0])
    g2 = mod.forward_grade(bars2, 0, "long", hold_bars=2)
    assert g2["mae_pct"] == pytest.approx(-10.0)
    # missing forward bar → ungradeable
    assert mod.forward_grade(bars, 3, "long") is None


def test_chg24_and_sma200_helpers():
    mod = _load_module()
    t = G0 + 100 * BAR1H
    closes = {t: 110.0, t - 24 * BAR1H: 100.0}
    assert mod._chg24_from_1h(closes, t + BAR1H) == pytest.approx(10.0)
    assert mod._chg24_from_1h({}, t + BAR1H) is None

    dts = [G0 + k * BAR1D for k in range(250)]
    dcl = [100.0 + k for k in range(250)]
    sma = mod._sma200_as_of(dts, dcl, dts[-1] + BAR1D)
    assert sma == pytest.approx(sum(dcl[-200:]) / 200)
    assert mod._sma200_as_of(dts[:100], dcl[:100], dts[-1]) is None


# ---------------------------------------------------------------------------
# Walk-forward segmentation
# ---------------------------------------------------------------------------

def test_wf_bounds_and_segments():
    mod = _load_module()
    b = mod.wf_bounds(0, 100)
    assert b == (60, 80)
    assert mod.segment_of(59, b) == "train"
    assert mod.segment_of(60, b) == "val"
    assert mod.segment_of(79, b) == "val"
    assert mod.segment_of(80, b) == "test"


# ---------------------------------------------------------------------------
# Arithmetic replay units
# ---------------------------------------------------------------------------

def test_replay_late_entry_block_thresholds():
    mod = _load_module()
    p = _params(mod)
    m = {"rsi4h": 80.0, "adx4h": 20.0, "extension": 1.0, "trend_dir": "flat"}
    assert mod.replay_late_entry_block(m, "long", p) is True
    assert mod.replay_late_entry_block(m, "short", p) is False
    # strong aligned trend relaxes the limits (82/±3.5)
    m2 = {"rsi4h": 78.0, "adx4h": 40.0, "extension": 2.0, "trend_dir": "bullish"}
    assert mod.replay_late_entry_block(m2, "long", p) is False
    p_no = _params(mod, trend_relax_enabled=False)
    assert mod.replay_late_entry_block(m2, "long", p_no) is True
    m3 = {"rsi4h": 20.0, "adx4h": 40.0, "extension": -3.0,
          "trend_dir": "bearish"}
    assert mod.replay_late_entry_block(m3, "short", p) is False
    assert mod.replay_late_entry_block(m3, "short", p_no) is True


def test_trend_filter_and_cap_replays():
    mod = _load_module()
    p = dict(mod.BASE_TREND)
    assert mod.replay_trend_filter_block({"above_sma": None}, p) is False
    assert mod.replay_trend_filter_block({"above_sma": True}, p) is False
    # qualified daily mover bypasses the below-200SMA block
    assert mod.replay_trend_filter_block(
        {"above_sma": False, "chg24": 20.0}, p) is False
    # parabolic (>30%) is NOT bypassed; weak move is NOT bypassed
    assert mod.replay_trend_filter_block(
        {"above_sma": False, "chg24": 35.0}, p) is True
    assert mod.replay_trend_filter_block(
        {"above_sma": False, "chg24": 5.0}, p) is True
    assert mod.replay_trend_filter_block(
        {"above_sma": False, "chg24": None}, p) is True
    off = dict(p, allow_daily_mover_long_bypass=False)
    assert mod.replay_trend_filter_block(
        {"above_sma": False, "chg24": 20.0}, off) is True
    assert mod.replay_daily_ext_cap_block({"chg24": 31.0}, 30.0) is True
    assert mod.replay_daily_ext_cap_block({"chg24": None}, 30.0) is False


def test_gate_stats_harmful_rate():
    mod = _load_module()
    cands = [{"net_pct": n} for n in (1.0, -2.0, 3.0, -4.0)]
    st = mod.gate_stats(cands, lambda c: c["net_pct"] < 0)
    assert st["blocked"]["n"] == 2
    assert st["harmful_rate"] == 0.0
    assert st["avoided_loss_per_block"] == pytest.approx(3.0)
    st2 = mod.gate_stats(cands, lambda c: c["net_pct"] > 0)
    assert st2["harmful_rate"] == 1.0


def test_plateau_pick_prefers_flat_middle():
    mod = _load_module()
    curve = [
        {"axis": 1, "ev": -0.50, "n": 100, "sign_consistent": True},
        {"axis": 2, "ev": -0.45, "n": 100, "sign_consistent": True},
        {"axis": 3, "ev": -0.40, "n": 100, "sign_consistent": True},
        {"axis": 4, "ev": 2.00, "n": 100, "sign_consistent": False},
    ]
    pick = mod.plateau_pick(curve)
    assert pick["axis"] == 2
    assert mod.plateau_pick(
        [{"axis": 1, "ev": -0.5, "n": 10, "sign_consistent": True}]) is None


def test_axis_curve_covers_all_grid_values_off_baseline():
    """Regression: the single-axis curve must find every grid value of the
    swept axis even when that value differs from baseline (the collapsed
    ext_ob/adx_floor curves came from a pre-filter that only exempted
    rsi_ob)."""
    mod = _load_module()

    def _row(ext, adx):
        return {
            "params": {"rsi_ob": mod.BASE_PARAMS["rsi_ob"], "ext_ob": ext,
                       "adx_floor": adx,
                       "relax": mod.BASE_PARAMS["trend_relax_enabled"]},
            "train": {"blocked": {"ev": -1.0, "n": 10}},
            "val": {"blocked": {"ev": -0.5, "n": 10}},
            "test": {"blocked": {"ev": -0.2, "n": 10}},
        }

    exts = [2.0, 2.25, 2.5, 2.75, 3.0]
    adxs = [35, 40, 45]
    rows = [_row(e, a) for e in exts for a in adxs]
    curve_ext = mod.axis_curve(rows, "ext_ob", exts)
    curve_adx = mod.axis_curve(rows, "adx_floor", adxs)
    assert [p["axis"] for p in curve_ext] == exts
    assert [p["axis"] for p in curve_adx] == adxs
    assert all(p["n"] == 30 and p["sign_consistent"] for p in curve_ext)


# ---------------------------------------------------------------------------
# Property lock: arithmetic replay == source-of-truth functions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("over", [
    {},
    {"rsi_ob": 70.0, "rsi_os": 30.0},
    {"ext_ob": 3.0, "ext_os": -3.0, "adx_trend_threshold": 45.0},
    {"trend_relax_enabled": False},
    {"rsi_ob": 80.0, "rsi_os": 20.0, "ext_ob": 2.0, "ext_os": -2.0},
])
def test_replay_matches_late_entry_check(side, over):
    mod = _load_module()
    bars = _candles(_regime_closes())
    p = _params(mod, **over)
    seen_block = False
    for i in range(30, len(bars)):
        window = bars[max(0, i - 99): i + 1]
        v = late_entry_check(window, None, side, p)
        assert v["data_ok"]
        m = {"rsi4h": v["rsi4h"], "adx4h": v["adx4h"],
             "extension": v["extension"], "trend_dir": v["trend_direction"]}
        assert mod.replay_late_entry_block(m, side, p) == v["block"]
        seen_block = seen_block or v["block"]
    assert seen_block  # the regime series actually triggers vetoes


@pytest.mark.parametrize("side", ["long", "short"])
def test_replay_matches_relax_tier_check(side):
    mod = _load_module()
    bars = _candles(_regime_closes())
    p = _params(mod)
    for i in range(30, len(bars)):
        window = bars[max(0, i - 99): i + 1]
        v = relax_tier_check(window, side, p)
        if not v["data_ok"] or v["adx4h"] is None or v["rsi4h"] is None:
            continue
        # relax_tier_check has no trend_direction key; take it from the
        # same-window late_entry_check (identical _assess_trend call).
        td = late_entry_check(window, None, side, p)["trend_direction"]
        m = {"rsi4h": v["rsi4h"], "adx4h": v["adx4h"],
             "extension": v["extension"], "trend_dir": td}
        rep = mod.replay_relax_tier(m, side, p)
        assert rep["rt_relax45"] == v["rt_relax45_would_block"]
        assert rep["rt_weak_rsi70"] == v["rt_weak_rsi70_would_block"]
        assert rep["rt_no_adx20"] == v["rt_no_adx20_would_block"]


# ---------------------------------------------------------------------------
# Point-in-time discipline + overlap matching
# ---------------------------------------------------------------------------

def test_candidate_measurements_are_point_in_time():
    mod = _load_module()
    closes = _regime_closes()
    bars_a = _candles(closes)
    bars_b = _candles(closes[:110] + [c * 0.5 for c in closes[110:]])
    kw = dict(closes_1h={}, daily_ts=[], daily_closes=[], side="long")
    ca = {c["i"]: c for c in mod.compute_candidates("AAA", bars_a, **kw)}
    cb = {c["i"]: c for c in mod.compute_candidates("AAA", bars_b, **kw)}
    assert ca and cb
    for i, c in ca.items():
        if i >= 100:  # only candidates decided BEFORE the divergent tail
            continue
        other = cb[i]
        for k in ("rsi4h", "adx4h", "extension", "trend_dir", "t_close"):
            assert other[k] == c[k]


def test_overlap_check_matches_live_row():
    mod = _load_module()
    bars = _candles(_regime_closes())
    cands = mod.compute_candidates("AAA", bars, {}, [], [], side="long")
    idx = {("AAA", c["i"], "long"): c for c in cands}
    pick = next(c for c in cands if c["i"] == 100)
    ts = bars[100].t + BAR4 + 1234  # inside bar 101 → deciding bar = 100
    rec = {"coin": "AAA", "timestamp": ts, "side": "long",
           "blocked": mod.replay_late_entry_block(pick, "long",
                                                  mod.BASE_PARAMS),
           "rsi4h": pick["rsi4h"], "adx4h": pick["adx4h"],
           "extension": pick["extension"]}
    ts_list = [b.t for b in bars]
    out = mod.overlap_check([rec], idx, {"AAA": ts_list})
    assert out["matched"] == 1
    assert out["indicator_diff"]["rsi4h"]["max"] == 0.0
    assert out["block_agree_rate"] == 1.0
    # a row whose deciding bar has no forward grade does not match
    late = dict(rec, timestamp=bars[-1].t + BAR4 + 1234)
    out2 = mod.overlap_check([late], idx, {"AAA": ts_list})
    assert out2["matched"] == 0


# ---------------------------------------------------------------------------
# End-to-end: dry-run writes nothing; --write emits one isolated report
# ---------------------------------------------------------------------------

def _seed_window(coin, as_of, n4=28 * 6, n1=12 * 24, nd=225):
    bars4 = _candles(_regime_closes(n4), t0=as_of - n4 * BAR4)
    px, bars1 = 100.0, []
    for k in range(n1):
        px *= 1.0 + 0.0005 * math.sin(k / 3.0)
        bars1.append(Candle(t=as_of - (n1 - k) * BAR1H, o=px, h=px * 1.001,
                            l=px * 0.999, c=px, v=1.0))
    barsd = _candles([100.0 + 0.05 * k for k in range(nd)],
                     t0=as_of - nd * BAR1D, bar=BAR1D)
    hc._BAR_CACHE[(coin, "4h")] = {b.t: b for b in bars4}
    hc._BAR_CACHE[(coin, "1h")] = {b.t: b for b in bars1}
    hc._BAR_CACHE[(coin, "1d")] = {b.t: b for b in barsd}
    return bars4


def test_run_dry_run_and_write(tmp_path):
    mod = _load_module()
    as_of = G0 + 400 * BAR4
    bars4 = _seed_window("AAA", as_of)
    live = tmp_path / "live.jsonl"
    rec = {"coin": "AAA", "timestamp": bars4[150].t + BAR4 + 999,
           "side": "long", "blocked": False, "entry_px": 1.0}
    live.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    before = live.read_bytes()
    rep_path = tmp_path / "report.json"

    rep = mod.run(days=10, coins=["AAA"], universe_file=str(live),
                  as_of_ms=as_of, write=False, out=str(rep_path))
    assert rep["_written"] is False
    assert not rep_path.exists()
    assert len(rep["ta_late_entry_sweep"]) == 150
    assert rep["overlap"]["matched"] == 1

    rep2 = mod.run(days=10, coins=["AAA"], universe_file=str(live),
                   as_of_ms=as_of, write=True, out=str(rep_path))
    assert rep2["_written"] is True
    saved = json.loads(rep_path.read_text(encoding="utf-8"))
    assert saved["n_candidates"] == rep2["n_candidates"]
    # the live input was never touched
    assert live.read_bytes() == before

    # evict mode drops raw bars but yields identical candidates
    rep3 = mod.run(days=10, coins=["AAA"], universe_file=str(live),
                   as_of_ms=as_of, write=False, out=str(rep_path),
                   evict_cache=True)
    assert rep3["n_candidates"] == rep["n_candidates"]
    assert rep3["overlap"]["matched"] == rep["overlap"]["matched"]
    assert ("AAA", "4h") not in hc._BAR_CACHE
