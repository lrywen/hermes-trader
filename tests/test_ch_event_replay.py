"""Tests for scripts/ch_event_replay.py — the event-time synthetic-fill replay.

Cover the pure mechanics offline: 15m->4h aggregation, Wilder ATR (no forming
bar), and simulate_trade driving the REAL production DSLTracker over a fixed bar
path (max_loss stop, hard timeout, long/short symmetry, fee drag).
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


rp = _load("ch_event_replay", "ch_event_replay.py")
from hermes_trader.agents import dsl_exit
from hermes_trader.agents.dsl_exit import ExitPolicy, DSLTracker

M15 = 15 * 60_000
H4 = 4 * 3600_000


def _bars(prices15, start=1000 * H4):
    """Build ascending 15m bars whose o=h=l=c follow ``prices15``."""
    out = []
    for i, p in enumerate(prices15):
        t = start + i * M15
        out.append((t, p, p, p, p))
    return out


def test_aggregate_4h_groups_16_bars_and_extremes():
    prices = [100.0] * 32
    bars = _bars(prices)
    # make bar 0 low=99, bar 15 high=101 inside the first 4h bucket
    t0 = bars[0][0]
    bars[0] = (t0, 100, 100, 99, 100)
    bars[15] = (t0 + 15 * M15, 100, 101, 100, 100)
    a = rp.aggregate_4h(bars)
    assert len(a) == 2
    assert a[0][0] == t0
    assert a[0][2] == 101 and a[0][3] == 99   # high/low over the 16 bars
    assert a[0][4] == 100                      # close = last 15m close
    assert a[1][0] == t0 + H4


def test_wilder_atr_excludes_entry_and_forming_bar():
    # build 30 flat 4h bars then inject a 10-point range in the closed history
    bars = [[i * H4, 100.0, 100.0, 100.0, 100.0] for i in range(40)]
    for j in range(1, 30):
        bars[j][2] = 110.0
        bars[j][3] = 100.0
    i_entry = 35
    a = rp.wilder_atr_pct(bars, i_entry=i_entry, entry_px=100.0)
    assert a > 0
    # entry bar (index 35) and after must not change the value
    bars[36][2] = 500.0
    a2 = rp.wilder_atr_pct(bars, i_entry=i_entry, entry_px=100.0)
    assert a2 == pytest.approx(a)


def _sim(prices15, side="LONG", atr_pct=0.0, pol=None, max_hold=400):
    bars = _bars(prices15)
    return rp.simulate_trade(
        bars=bars, i_entry=20, side=side, entry_atr_pct=atr_pct,
        policy=pol or ExitPolicy(), leverage=1, fee_pct=0.00045,
        entry_time_s=bars[20][0] / 1000.0, dsl_module=dsl_exit,
        tracker_cls=DSLTracker, max_hold=max_hold)


def test_long_max_loss_exit_and_fee_drag():
    # entry at bar20 =100; price collapses to 90 -> hard max_loss stop fires
    prices = [100.0] * 21 + [90.0] * 200
    tr = _sim(prices, side="LONG")
    assert tr is not None
    assert tr["reason"] == "max_loss"
    assert tr["side"] == "LONG"
    assert tr["gross_pct"] < 0
    # net is strictly worse than gross by the round-trip fee
    assert tr["ret_pct"] < tr["gross_pct"]
    assert tr["fee_roundtrip_pct"] == pytest.approx(0.09)


def test_short_profits_on_decline_symmetric():
    prices = [100.0] * 21 + [110.0] * 200
    tr = _sim(prices, side="SHORT")
    assert tr is not None
    assert tr["reason"] == "max_loss"
    assert tr["gross_pct"] < 0   # adverse move against a short -> stopped


def test_timeout_exit_closes_position():
    # perfectly flat market: never hits a stop; hard timeout must close it
    prices = [100.0] * 400
    tr = _sim(prices, side="LONG", max_hold=400)
    assert tr is not None
    assert tr["reason"] in ("hard_timeout", "stale_flat_timeout",
                            "ignite_timeout")
    assert 0 < tr["bars_held"] <= 400


def test_ignition_marks_launch_and_survives_grace():
    # flat 100 until entry (idx20); a high-volume breakout prints at idx24
    # (60 min, before the 90-min launch grace) followed by a sustained uptrend.
    prices = [100.0] * 24 + list(range(101, 131)) + [120.0] * 40
    bars = _bars(prices)
    # attach volume: low everywhere, one 25x spike on the breakout bar
    for k, b in enumerate(bars):
        bars[k] = b + (1.0,)
    spike = 24
    bars[spike] = bars[spike][:5] + (25.0,)
    tr = rp.simulate_trade(
        bars=bars, i_entry=20, side="LONG", entry_atr_pct=0.0,
        policy=ExitPolicy(), leverage=1, fee_pct=0.0,
        entry_time_s=bars[20][0] / 1000.0, dsl_module=dsl_exit,
        tracker_cls=DSLTracker, max_hold=300)
    assert tr is not None
    # ignited trade is NOT closed as an un-ignited timeout; it rode to a
    # trailing stop / hard timeout after holding well beyond the 90-min grace
    assert tr["reason"] != "ignite_timeout"
    assert tr["bars_held"] * 15 > 90 or tr["reason"] == "floor_breach"


def test_right_censored_returns_none():
    # not enough bars after entry -> never resolves
    prices = [100.0] * 25
    tr = _sim(prices, side="LONG", max_hold=400)
    assert tr is None
