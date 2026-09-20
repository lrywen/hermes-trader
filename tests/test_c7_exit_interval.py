"""C-7: guard the --exit-interval 1h parameterization of bt_ra_exch.

The higher-timeframe evaluation reuses ``_simulate_trade`` with a ``bar_ms``
argument instead of forking a third exit implementation. These tests pin:

* 5m default is byte-identical to explicitly passing MS_5M (the parameterization
  did not perturb the already-accepted 5m kernel);
* wall-clock exits (time_scratch / hard timeout) are calibrated in REAL minutes
  via bar_ms — a 60-min scratch fires after 12 bars on 5m but after 1 bar on 1h;
* PIT de-duplication in ``_dispatch_arms_1h`` maps several same-hour 5m signals
  to ONE fill on the next 1h open (no mixed-frequency look-ahead).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def btx():
    path = _REPO / "scripts" / "bt_ra_exch.py"
    spec = importlib.util.spec_from_file_location("bt_ra_exch_c7", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _flat_bars(btx, n, step_ms, t0=0, px=100.0):
    C = btx.Candle
    return [C(t=t0 + i * step_ms, o=px, h=px, l=px, c=px, v=1.0)
            for i in range(n)]


def _scratch_dsl(btx):
    # 60-min scratch only fires when peak < 0.3%; flat price satisfies it.
    return btx.DslParams(
        max_loss_pct=5.0, protect_pct=1.5, retrace_threshold=0.4,
        hard_timeout_minutes=100000, breakeven_trigger_pct=99,
        breakeven_lock_pct=0.3, stale_flat_timeout_minutes=100000,
        phase2_tiers=[(2.0, 0.35)], time_scratch_minutes=60,
        time_scratch_min_peak=0.3)


def _cand(btx):
    return btx.Candidate(bar_idx=0, side="long", arm="filt", score=70, fired=[])


def test_5m_default_equals_explicit_bar_ms(btx):
    bars = _flat_bars(btx, 40, btx.MS_5M)
    dsl = _scratch_dsl(btx)
    kw = dict(dsl=dsl, notional=10_000.0, entry_slip=0.0, exit_slip=0.0,
              stop_delay=0.0, coin="TST")
    a = btx._simulate_trade(_cand(btx), bars, 0, bar_ms=btx.MS_5M, **kw)
    b = btx._simulate_trade(_cand(btx), bars, 0, **kw)  # default
    assert a is not None and b is not None
    for f in ("exit_t", "exit_px", "exit_reason", "hold_bars",
              "pnl_gross", "pnl_net", "peak_pct"):
        assert getattr(a, f) == getattr(b, f), f


def test_wallclock_scratch_fires_after_12_bars_on_5m(btx):
    bars = _flat_bars(btx, 40, btx.MS_5M)
    tr = btx._simulate_trade(_cand(btx), bars, 0, dsl=_scratch_dsl(btx),
                             notional=10_000.0, entry_slip=0.0, exit_slip=0.0,
                             stop_delay=0.0, coin="TST", bar_ms=btx.MS_5M)
    # Signal bar 0 -> fill bar 1 (t=5m). scratch 60m elapsed at bar whose
    # (t+5m-entry)/60000 >= 60 → entry bar index 1 + 11 more → exit bar idx 12.
    assert tr.exit_reason == "time_scratch"
    assert tr.hold_bars == 12


def test_wallclock_scratch_fires_after_1_bar_on_1h(btx):
    bars = _flat_bars(btx, 40, btx.MS_1H)
    tr = btx._simulate_trade(_cand(btx), bars, 0, dsl=_scratch_dsl(btx),
                             notional=10_000.0, entry_slip=0.0, exit_slip=0.0,
                             stop_delay=0.0, coin="TST", bar_ms=btx.MS_1H)
    # Fill bar 1 (t=1h): elapsed at that bar = (1h+1h-1h)/60000 = 60m → fires.
    assert tr.exit_reason == "time_scratch"
    assert tr.hold_bars == 1


def test_dispatch_1h_dedupes_same_hour_signals_to_one_fill(btx):
    # 12 flat 5m bars inside ONE hour, then hours; one 1h series aligned to t=0.
    bars5m = _flat_bars(btx, 24, btx.MS_5M)
    h1 = _flat_bars(btx, 6, btx.MS_1H)
    # Two filt signals at 5m indices 0 and 2 — both decide before the 1h bar
    # OPENING at t=1h (idx1); only the later (idx2) survives, one fill total.
    per_arm = {"filt": [(0, _cand(btx)), (2, _cand(btx))]}
    dsls = {"live": _scratch_dsl(btx), "filt": _scratch_dsl(btx)}
    P = {"exch": None, "sizing": None}
    trades, funnel = btx._dispatch_arms_1h(
        "TST", per_arm, bars5m, h1, dsls, 10_000.0, 0.0, 0.0, 0.0, P,
        lambda ms: "neutral", lambda ms: 0.0, {"skip_open_pos": 0})
    filt = [t for t in trades if t.arm == "filt"]
    assert len(filt) == 1                      # one hour → one fill
    assert filt[0].entry_t == btx.MS_1H       # filled at next 1h open, not 5m


def test_dispatch_1h_signal_after_1h_open_waits_next_bar(btx):
    # A 5m signal deciding at t=1h:05 (idx12) cannot fill at the 1h bar that
    # already opened at t=1h (would contain pre-signal prices) → fills t=2h.
    bars5m = _flat_bars(btx, 24, btx.MS_5M)
    h1 = _flat_bars(btx, 6, btx.MS_1H)
    cand = btx.Candidate(bar_idx=12, side="long", arm="filt", score=70, fired=[])
    per_arm = {"filt": [(12, cand)]}
    dsls = {"live": _scratch_dsl(btx), "filt": _scratch_dsl(btx)}
    trades, _ = btx._dispatch_arms_1h(
        "TST", per_arm, bars5m, h1, dsls, 10_000.0, 0.0, 0.0, 0.0,
        {"exch": None, "sizing": None}, lambda ms: "neutral",
        lambda ms: 0.0, {"skip_open_pos": 0})
    assert len(trades) == 1
    assert trades[0].entry_t == 2 * btx.MS_1H   # PIT: next hour, no look-ahead
