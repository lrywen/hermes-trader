"""Tests for the S1 dual-period PF report (scripts/pf_dual_period_report.py).

The script is a standalone CLI under scripts/ (not a package module), so it
is loaded via importlib like test_p3_14_ip_drift_watch.py. Only the pure
core functions are unit-tested; live candle fetching is never called.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_module():
    name = "pf_dual_period_report"
    spec = importlib.util.spec_from_file_location(
        name, _SCRIPTS_DIR / "pf_dual_period_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # required so @dataclass can resolve the module
    spec.loader.exec_module(mod)
    return mod


pf = _load_module()


# ── parsing / cost ───────────────────────────────────────────────────────────

def test_parse_window_valid():
    assert pf.parse_window("4h:365d") == ("4h", 365)
    assert pf.parse_window("1h:180d") == ("1h", 180)


@pytest.mark.parametrize("bad", ["4h365d", "4x:365d", "4h:365", "4h:0d", "4h:-5d"])
def test_parse_window_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        pf.parse_window(bad)


def test_round_trip_cost_pct():
    # (2.5bps taker + 2bps slip) x 2 fills / 100 -> 0.09%
    assert pf.round_trip_cost_pct(2.5, 2.0) == pytest.approx(0.09)
    assert pf.round_trip_cost_pct(0.0, 0.0) == 0.0


def test_bars_needed():
    # 4h = 6 bars/day; 365d + 120 warmup + 6 hold + 1 tail
    assert pf.bars_needed("4h", 365, 6) == 365 * 6 + 120 + 6 + 1
    assert pf.bars_needed("1h", 180, 24) == 180 * 24 + 120 + 24 + 1


# ── forward matching ─────────────────────────────────────────────────────────

def test_forward_trade_long_and_short():
    gross, net = pf.forward_trade(100.0, 110.0, "long", 0.09)
    assert gross == pytest.approx(10.0)
    assert net == pytest.approx(9.91)
    # short profits when price falls
    gross_s, net_s = pf.forward_trade(100.0, 90.0, "short", 0.09)
    assert gross_s == pytest.approx(10.0)
    assert net_s == pytest.approx(9.91)
    # short loses when price rises
    gross_l, _ = pf.forward_trade(100.0, 110.0, "short", 0.0)
    assert gross_l == pytest.approx(-10.0)


# ── profit factor ────────────────────────────────────────────────────────────

def test_profit_factor_basic():
    assert pf.profit_factor([10.0, -5.0]) == pytest.approx(2.0)
    assert pf.profit_factor([10.0, 10.0, -5.0, -5.0]) == pytest.approx(2.0)


def test_profit_factor_no_losses_returns_none():
    assert pf.profit_factor([1.0, 2.0, 3.0]) is None
    assert pf.profit_factor([]) is None


def test_profit_factor_breakeven_counts_as_loss():
    # ties (<=0) go to the loss bucket: 10 / |0| -> None (no negative losses),
    # and an all-tie/all-loss book yields 0.0.
    assert pf.profit_factor([10.0, 0.0]) is None
    assert pf.profit_factor([-1.0, -2.0]) == 0.0


# ── replay on synthetic candles ──────────────────────────────────────────────

def _mk_candles(n: int, growth: float = 1.01, start: float = 100.0):
    candles = []
    for i in range(n):
        c = start * (growth ** i)
        o = start * (growth ** max(0, i - 1))
        candles.append(pf.Candle(t=i * 3_600_000, o=o, h=c * 1.001,
                                 l=c * 0.999, c=c, v=1000.0))
    return candles


def test_replay_signals_uptrend_fires_long_only():
    candles = _mk_candles(80, growth=1.02)  # strong steady uptrend
    trades = pf.replay_signals(candles, pf.SIGNAL_SPECS, hold_bars=3,
                               cost_pct=0.09, warmup=30)
    assert trades, "expected signals to fire on a strong uptrend"
    assert all(t.side == "long" for t in trades)
    assert {t.source for t in trades}  # non-empty source set
    # every trade must exit within the series and carry a forward window
    for t in trades:
        assert t.gross_pct > 0  # strong uptrend held to exit
        assert t.net_pct == pytest.approx(t.gross_pct - 0.09)


def test_replay_signals_downtrend_fires_short():
    candles = _mk_candles(80, growth=0.98)  # steady decline
    trades = pf.replay_signals(candles, pf.SIGNAL_SPECS, hold_bars=3,
                               cost_pct=0.0, warmup=30)
    shorts = [t for t in trades if t.side == "short"]
    assert shorts, "downtrend_momentum should fire SHORT on a decline"
    assert all(t.gross_pct > 0 for t in shorts)


def test_replay_signals_respects_warmup_and_hold_bounds():
    candles = _mk_candles(60, growth=1.02)
    hold = 5
    trades = pf.replay_signals(candles, pf.SIGNAL_SPECS, hold_bars=hold,
                               cost_pct=0.0, warmup=40)
    # signal bar i in [40, len-hold-2): entry_ts must be >= warmup bar time
    for t in trades:
        assert t.entry_ts >= candles[40].t
    # no trade may reference an exit bar beyond the fetched series
    assert all(t.exit_ts <= candles[-1].t for t in trades)


def test_replay_signals_entry_exit_timing_no_lookahead():
    # Hand-check one firing instance of uptrend_momentum on a known series.
    candles = _mk_candles(100, growth=1.03)
    spec = next(s for s in pf.SIGNAL_SPECS if s.source == "uptrend_momentum")
    trades = pf.replay_signals(candles, [spec], hold_bars=2, cost_pct=0.0,
                               warmup=80)
    assert trades
    t = trades[0]
    # signal fired on closed bar i; entry at i+1 open, exit at i+2 close
    sig_i = next(i for i in range(len(candles)) if candles[i].t == t.entry_ts)
    entry_px = candles[sig_i + 1].o
    exit_px = candles[sig_i + 2].c
    expected = (exit_px - entry_px) / entry_px * 100.0
    assert t.gross_pct == pytest.approx(expected, rel=1e-9)


def test_replay_signals_only_since_ts():
    candles = _mk_candles(100, growth=1.02)
    cutoff = candles[80].t
    trades = pf.replay_signals(candles, pf.SIGNAL_SPECS, hold_bars=3,
                               cost_pct=0.0, warmup=30, only_since_ts=cutoff)
    assert trades
    assert all(t.entry_ts >= cutoff for t in trades)


# ── aggregation / gate ───────────────────────────────────────────────────────

def _trades(pcts, source="x", side="long"):
    return [pf.Trade(source=source, side=side, coin="BTC", entry_ts=i,
                     gross_pct=g, net_pct=g - 0.09)
            for i, g in enumerate(pcts)]


def test_pf_stats_aggregates_and_halves():
    trades = _trades([10.0, -5.0, 8.0, -4.0])
    st = pf.pf_stats(trades)
    assert st.n == 4
    assert st.gross_pf == pytest.approx(18.0 / 9.0)
    # half split: first 2 vs last 2, each with one win one loss
    assert st.gross_first_half == pytest.approx(2.0)
    assert st.gross_second_half == pytest.approx(2.0)


def test_gate_verdict_pass_fail_insufficient():
    good = pf.PFStats(n=50, gross_pf=1.5, net_pf=1.3,
                      gross_first_half=1.4, gross_second_half=1.4,
                      net_first_half=1.2, net_second_half=1.2)
    bad = pf.PFStats(n=50, gross_pf=1.1, net_pf=0.9,
                     gross_first_half=1.0, gross_second_half=1.0,
                     net_first_half=0.8, net_second_half=0.9)
    thin = pf.PFStats(n=10, gross_pf=2.0, net_pf=1.9,
                      gross_first_half=None, gross_second_half=None,
                      net_first_half=None, net_second_half=None)
    assert pf.gate_verdict([good, good], 30, 1.05) == "PASS"
    assert pf.gate_verdict([good, bad], 30, 1.05) == "FAIL"
    assert pf.gate_verdict([good, thin], 30, 1.05) == "INSUFFICIENT"
    # no losses -> None net PF clears the gate
    noloss = pf.PFStats(n=50, gross_pf=None, net_pf=None,
                        gross_first_half=None, gross_second_half=None,
                        net_first_half=None, net_second_half=None)
    assert pf.gate_verdict([noloss, noloss], 30, 1.05) == "PASS"


# ── measured slippage ────────────────────────────────────────────────────────

def test_measured_slip_bps_mean_and_floor():
    now = time.time()
    mem = {"closes": [
        {"coin": "BTC", "exit_slip_bps": 2.0, "closed_at": now},
        {"coin": "BTC", "exit_slip_bps": 4.0, "closed_at": now},
        {"coin": "BTC", "exit_slip_bps": 6.0, "closed_at": now},
        {"coin": "ETH", "exit_slip_bps": 9.0, "closed_at": now},
    ]}
    assert pf.measured_slip_bps(mem, "BTC") == pytest.approx(4.0)
    # fewer than min_samples adverse fills -> 0.0 (caller applies fallback)
    assert pf.measured_slip_bps(mem, "ETH") == 0.0
    # stale rows outside the window are ignored
    mem_old = {"closes": [{"coin": "BTC", "exit_slip_bps": 5.0,
                           "closed_at": now - 60 * 86400}]}
    assert pf.measured_slip_bps(mem_old, "BTC") == 0.0


def test_module_exposes_cli():
    assert hasattr(pf, "main")
    assert callable(pf.main)
    assert len(pf.SIGNAL_SPECS) >= 8
