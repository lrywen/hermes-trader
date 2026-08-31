"""Trade-outcome store: record_close → win-rate / payoff → risk-of-ruin.

Uses a fresh AgentMemory instance with flush() stubbed so the live
.agent-memory.json is never touched.
"""

from hermes_trader.agents.memory import AgentMemory
from hermes_trader.agents.sizing import risk_of_ruin


def _mem():
    m = AgentMemory()
    # P1-6: flush() gained (force=...) and record_close passes force=True;
    # accept any signature so the stub never touches disk.
    m.flush = lambda *a, **k: None  # never write to disk in tests
    return m


def _close(pnl_pct, spot=None):
    return {
        "coin": "X", "side": "long", "entry_px": 100.0, "exit_px": 101.0,
        "realized_pnl_pct": pnl_pct, "spot_pct": spot if spot is not None else pnl_pct,
        "realized_pnl_usd": pnl_pct, "leverage": 1, "closed_at": 0,
    }


def test_record_close_feeds_win_rate():
    m = _mem()
    for p in (5.0, -2.0, 8.0, -3.0):  # 2 wins, 2 losses
        m.record_close(_close(p))
    wr = m.get_win_rate()
    assert wr["total"] == 4
    assert wr["wins"] == 2
    assert wr["rate"] == 0.5


def test_payoff_stats_computed():
    m = _mem()
    for p in (10.0, 10.0, -5.0):  # avg win 10, avg loss 5 -> payoff 2.0, win_rate 2/3
        m.record_close(_close(p))
    s = m.get_payoff_stats()
    assert s["n"] == 3
    assert abs(s["win_rate"] - 2 / 3) < 1e-9
    assert abs(s["avg_win_pct"] - 10.0) < 1e-9
    assert abs(s["avg_loss_pct"] - 5.0) < 1e-9
    assert abs(s["payoff_ratio"] - 2.0) < 1e-9


def test_payoff_stats_empty_is_zero_not_crash():
    s = _mem().get_payoff_stats()
    assert s == {"n": 0, "win_rate": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "payoff_ratio": 0.0}


def test_outcome_store_drives_risk_of_ruin():
    m = _mem()
    # A solid edge: 60% win, payoff 2.0
    for _ in range(6):
        m.record_close(_close(10.0))
    for _ in range(4):
        m.record_close(_close(-5.0))
    s = m.get_payoff_stats()
    ror = risk_of_ruin(win_rate=s["win_rate"], payoff_ratio=s["payoff_ratio"], risk_per_trade_pct=0.0075)
    assert s["win_rate"] == 0.6 and abs(s["payoff_ratio"] - 2.0) < 1e-9
    assert 0.0 <= ror < 0.05  # strong edge + small risk -> low ruin


def test_cap_bounds_closes_list():
    m = _mem()
    from hermes_trader.agents import memory as _memmod
    for i in range(_memmod.MAX_CLOSES + 50):
        m.record_close(_close(1.0))
    assert len(m._closes) == _memmod.MAX_CLOSES


def test_get_win_rate_falls_back_when_no_closes():
    m = _mem()  # no closes recorded
    wr = m.get_win_rate()
    assert wr["total"] == 0 and wr["rate"] == 0


# ── O-8 (supplemental audit 2026-08-30): measured round-trip fee ──────────

def _fee_close(fee_usd, notional_usd, *, fee_actual, coin="F"):
    """A close row carrying (or not) real exchange fees. closed_at=now (ms)
    so it falls inside the 30-day lookback window."""
    import time as _t
    return {
        "coin": coin, "side": "long", "entry_px": 100.0, "exit_px": 100.0,
        "realized_pnl_pct": 0.0, "spot_pct": 0.0, "realized_pnl_usd": 0.0,
        "leverage": 1, "closed_at": int(_t.time() * 1000),
        "fee_usd": fee_usd, "notional_usd": notional_usd,
        "fee_actual": fee_actual,
    }


def test_avg_round_trip_fee_bps_from_real_closes():
    # $1000 notional, $0.40/$0.60/$0.50 round-trip fees → 4/6/5 bps → mean 5.
    m = _mem()
    m.record_close(_fee_close(0.40, 1000.0, fee_actual=True))
    m.record_close(_fee_close(0.60, 1000.0, fee_actual=True))
    m.record_close(_fee_close(0.50, 1000.0, fee_actual=True))
    bps = m.avg_round_trip_fee_bps("F")
    assert abs(bps - 5.0) < 1e-9


def test_avg_fee_ignores_modeled_close_rows():
    # In-process DSL closes model fee_usd as 2.5bpsx2 and are NOT flagged
    # fee_actual — they must not calibrate the backtest constant (circular).
    m = _mem()
    for _ in range(5):
        m.record_close(_fee_close(0.05, 1000.0, fee_actual=False))
    # Insufficient REAL samples → 0.0 (caller keeps the conservative default).
    assert m.avg_round_trip_fee_bps("F") == 0.0


def test_avg_fee_below_min_samples_returns_zero():
    m = _mem()
    m.record_close(_fee_close(0.40, 1000.0, fee_actual=True))
    m.record_close(_fee_close(0.60, 1000.0, fee_actual=True))
    # Only 2 real samples < min_samples=3 → no calibration on noise.
    assert m.avg_round_trip_fee_bps("F") == 0.0
