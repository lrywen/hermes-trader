"""Unit tests for scripts/pf_multitf_ab_report.py (strategy-direction S3).

Covers the pure multi-timeframe machinery: epoch-aligned HTF resampling,
strict no-look-ahead bucket closure, HTF ema/rsi/adx feature extraction,
the A (direction agreement) and B (agreement + strength + non-extreme RSI)
admission predicates, the end-to-end replay filter, and the drawdown metric.

Loads pf_dual_period_report first (sys.modules registered) then the
multi-tf report, mirroring test_param_robustness_report.py.
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
mt = _load("pf_multitf_ab_report", "pf_multitf_ab_report.py")

_H1 = pf._INTERVAL_MS["1h"]  # 3_600_000
_H4 = pf._INTERVAL_MS["4h"]  # 14_400_000


def _candles(n: int, start_t: int, step_ms: int, price_path) -> list:
    """Build n candles; price_path(i) gives close (o/h/l derived around it)."""
    out = []
    for i in range(n):
        px = float(price_path(i))
        out.append(mt.Candle(t=start_t + i * step_ms, o=px, h=px + 1.0,
                             l=px - 1.0, c=px, v=10.0))
    return out


# ── resampling ───────────────────────────────────────────────────────────────

def test_resample_htf_groups_epoch_aligned_buckets():
    # 8 hourly bars starting exactly on a 4h boundary -> two 4h buckets.
    base = _candles(8, 0, _H1, lambda i: 100.0 + i)
    htf = mt.resample_htf(base, factor=4, base_interval_ms=_H1)
    assert len(htf) == 2
    assert htf[0].t == 0
    assert htf[1].t == _H4
    assert htf[0].o == pytest.approx(100.0)
    assert htf[0].c == pytest.approx(103.0)
    assert htf[0].h == pytest.approx(104.0)   # max high = last close+1
    assert htf[0].l == pytest.approx(99.0)    # min low = first close-1
    assert htf[0].v == pytest.approx(40.0)


def test_resample_htf_misaligned_start_still_epoch_buckets():
    # Starting mid-bucket still buckets by epoch (t//bucket_ms), not by index.
    base = _candles(8, _H1, _H1, lambda i: 100.0 + i)  # t = 1h,2h,...,8h
    htf = mt.resample_htf(base, factor=4, base_interval_ms=_H1)
    # 8 misaligned bars fall in three epoch buckets: [0] holds 1h/2h/3h,
    # [4h] holds 4h..7h, [8h] holds the single 8h bar.
    assert [c.t for c in htf] == [0, _H4, 2 * _H4]
    assert htf[0].v == pytest.approx(30.0)   # 3 bars in the partial first bucket
    assert htf[1].v == pytest.approx(40.0)
    assert htf[2].v == pytest.approx(10.0)


# ── no look-ahead bucket closure ─────────────────────────────────────────────

def test_closed_htf_window_excludes_incomplete_bucket():
    base = _candles(8, 0, _H1, lambda i: 100.0 + i)
    htf = mt.resample_htf(base, factor=4, base_interval_ms=_H1)
    # After bar index 2 closes (t=3h), no 4h bucket starting at 0 is complete.
    close_at_bar2 = base[2].t + _H1  # 3h
    assert mt.closed_htf_window(htf, 4, _H1, close_at_bar2) == []
    # After bar index 3 closes (t=4h), bucket [0,4h) is complete.
    close_at_bar3 = base[3].t + _H1  # 4h
    closed = mt.closed_htf_window(htf, 4, _H1, close_at_bar3)
    assert len(closed) == 1
    assert closed[0].t == 0


# ── HTF features ─────────────────────────────────────────────────────────────

def test_htf_features_none_when_insufficient_bars():
    closes = _candles(20, 0, _H4, lambda i: 100.0 + i)
    assert mt.htf_features(closes) is None


def test_htf_features_bullish_and_bearish():
    up = _candles(40, 0, _H4, lambda i: 100.0 + i * 2.0)
    feat_up = mt.htf_features(up)
    assert feat_up is not None and feat_up["bullish"] == 1.0
    down = _candles(40, 0, _H4, lambda i: 200.0 - i * 2.0)
    feat_dn = mt.htf_features(down)
    assert feat_dn is not None and feat_dn["bullish"] == 0.0


# ── admission predicates ─────────────────────────────────────────────────────

def _feat(bullish, adx=30.0, rsi=50.0):
    return {"bullish": 1.0 if bullish else 0.0, "adx": adx, "rsi": rsi}


def test_admit_agreement_requires_matching_trend():
    assert mt.admit_agreement("long", _feat(True)) is True
    assert mt.admit_agreement("short", _feat(False)) is True
    assert mt.admit_agreement("long", _feat(False)) is False
    assert mt.admit_agreement("short", _feat(True)) is False
    assert mt.admit_agreement("long", None) is False


def test_admit_strength_requires_adx_threshold():
    assert mt.admit_strength_non_extreme("long", _feat(True, adx=25.0, rsi=50.0)) is True
    assert mt.admit_strength_non_extreme("long", _feat(True, adx=15.0, rsi=50.0)) is False


def test_admit_strength_blocks_extreme_rsi():
    # Long into HTF overbought is a top chase -> reject.
    assert mt.admit_strength_non_extreme("long", _feat(True, rsi=75.0)) is False
    # Short into HTF oversold is bottom catching -> reject.
    assert mt.admit_strength_non_extreme("short", _feat(False, rsi=25.0)) is False
    # Healthy extremes are admitted.
    assert mt.admit_strength_non_extreme("long", _feat(True, rsi=65.0)) is True
    assert mt.admit_strength_non_extreme("short", _feat(False, rsi=35.0)) is True


# ── end-to-end replay filter ─────────────────────────────────────────────────

def _always_long_spec():
    return pf.SignalSpec("always_long", lambda w: {"fired": True},
                         lambda h: "long")


def test_replay_baseline_unfiltered_but_a_requires_uptrend():
    factor, hold, warmup = 4, 3, 85
    # Strong uptrend: longs agree with HTF bullish -> A admits all.
    up = _candles(200, 0, _H1, lambda i: 100.0 + i * 0.5)
    tr_up = mt.replay_multitf(up, [_always_long_spec()], factor, _H1,
                              hold, 0.0, warmup)
    base_up = [t for t in tr_up if t.variant == "baseline"]
    a_up = [t for t in tr_up if t.variant == "A_agree"]
    assert len(base_up) > 0
    assert len(a_up) == len(base_up)

    # Strong downtrend: longs disagree with HTF bearish -> A and B admit none.
    dn = _candles(200, 0, _H1, lambda i: 300.0 - i * 0.5)
    tr_dn = mt.replay_multitf(dn, [_always_long_spec()], factor, _H1,
                              hold, 0.0, warmup)
    assert len([t for t in tr_dn if t.variant == "baseline"]) > 0
    assert [t for t in tr_dn if t.variant == "A_agree"] == []
    assert [t for t in tr_dn if t.variant == "B_agree_str"] == []


def test_replay_b_is_subset_of_a():
    factor, hold, warmup = 4, 3, 85
    up = _candles(200, 0, _H1, lambda i: 100.0 + i * 0.5)
    tr = mt.replay_multitf(up, [_always_long_spec()], factor, _H1,
                           hold, 0.0, warmup)
    a = {t.entry_ts for t in tr if t.variant == "A_agree"}
    b = {t.entry_ts for t in tr if t.variant == "B_agree_str"}
    assert b <= a  # B can only drop trades relative to A, never add them.


# ── drawdown ─────────────────────────────────────────────────────────────────

def test_equity_drawdown_peak_to_trough():
    def mk(ts, net):
        return mt.MTTrade(source="s", side="long", coin="BTC",
                          variant="baseline", entry_ts=ts,
                          gross_pct=net, net_pct=net, exit_ts=ts + 1)
    # cum: +10 (peak 10), -20 -> -10 (dd 20), +5 -> -5 (dd 15) => mdd 20.
    trades = [mk(1, 10.0), mk(2, -20.0), mk(3, 5.0)]
    assert mt.equity_drawdown(trades) == pytest.approx(20.0)


def test_empty_drawdown_is_zero():
    assert mt.equity_drawdown([]) == 0.0
