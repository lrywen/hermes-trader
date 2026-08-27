"""P3-4: the in-process memory growth lists must be FIFO-bounded.

The four append-only lists on AgentMemory (perceptions / analyses / trades /
closes) have config-driven caps (``memory_limits.*``) enforced at each append
site so memory cannot grow without bound; closes already had a cap test in
test_outcome_store.py, these cover the other three lists end to end through
the record_* methods.

Uses a fresh AgentMemory instance with flush() stubbed so neither the live
.agent-memory.json nor the events feed is touched.
"""

import pytest

from hermes_trader.agents.memory import AgentMemory
from hermes_trader.agents.config_store import (
    read_agent_config,
    write_agent_config,
)


def _mem():
    m = AgentMemory()
    # Never touch disk: stub the persistence flush and the authoritative
    # event feed (record_trade otherwise appends an "order" event).
    m.flush = lambda *a, **k: None
    return m


@pytest.fixture()
def small_limits():
    """Pin tiny memory_limits for the three lists under test, restoring the
    effective config afterwards so other tests keep production caps."""
    cfg = read_agent_config()
    saved = cfg.get("memory_limits")
    cfg["memory_limits"] = {
        "max_perceptions": 10,
        "max_analyses": 10,
        "max_trades": 10,
        "max_closes": 500,
    }
    write_agent_config(cfg, backup=False)
    yield
    restored = read_agent_config()
    if saved is None:
        restored.pop("memory_limits", None)
    else:
        restored["memory_limits"] = saved
    write_agent_config(restored, backup=False)


def test_cap_bounds_perceptions_list(small_limits):
    m = _mem()
    for i in range(15):
        m.record_perception({"seq": i})
    assert len(m._perceptions) == 10
    # FIFO: the oldest entries were evicted, the newest retained.
    assert m._perceptions[0]["seq"] == 5
    assert m._perceptions[-1]["seq"] == 14


def test_cap_bounds_analyses_list(small_limits):
    m = _mem()
    for i in range(15):
        m.record_analysis({"seq": i})
    assert len(m._analyses) == 10
    assert m._analyses[0]["seq"] == 5
    assert m._analyses[-1]["seq"] == 14


def test_cap_bounds_trades_list(small_limits, monkeypatch):
    # record_trade emits an authoritative "order" event into the events feed
    # (imported lazily as hermes_trader.event_log); stub the append so the
    # test never touches the events file.
    from hermes_trader import event_log
    monkeypatch.setattr(event_log, "append", lambda *a, **k: True)
    m = _mem()
    for i in range(15):
        m.record_trade({"seq": i, "coin": "BTC"})
    assert len(m._trades) == 10
    assert m._trades[0]["seq"] == 5
    assert m._trades[-1]["seq"] == 14


# ── R9/P3-4: age-based eviction ─────────────────────────────────────────────

@pytest.fixture()
def age_limits():
    """Pin a 1-day max age for perceptions/analyses and leave trades at the
    count-capped default (max_age_days.trades=0 → no age eviction)."""
    cfg = read_agent_config()
    saved = cfg.get("memory_limits")
    ml = dict(saved or {})
    ml["max_age_days"] = {"perceptions": 1, "analyses": 1, "trades": 0}
    cfg["memory_limits"] = ml
    write_agent_config(cfg, backup=False)
    yield
    restored = read_agent_config()
    if saved is None:
        restored.pop("memory_limits", None)
    else:
        restored["memory_limits"] = saved
    write_agent_config(restored, backup=False)


def test_age_evicts_old_perceptions(age_limits):
    import time as _time
    now_ms = int(_time.time() * 1000)
    day_ms = 86400 * 1000
    m = _mem()
    m.record_perception({"seq": "old", "ts": now_ms - 5 * day_ms})
    m.record_perception({"seq": "fresh", "ts": now_ms})
    m.record_perception({"seq": "undatable"})  # no timestamp → kept
    seqs = {p["seq"] for p in m._perceptions}
    assert seqs == {"fresh", "undatable"}


def test_age_evicts_old_analyses(age_limits):
    import time as _time
    now_ms = int(_time.time() * 1000)
    day_ms = 86400 * 1000
    m = _mem()
    m.record_analysis({"seq": "old", "created_at": now_ms - 3 * day_ms})
    m.record_analysis({"seq": "fresh", "created_at": now_ms})
    seqs = {a["seq"] for a in m._analyses}
    assert seqs == {"fresh"}


def test_trades_not_age_evicted(age_limits, monkeypatch):
    # trades max_age_days defaults to 0 (audit record): an old trade is kept
    # and only the count cap applies.
    from hermes_trader import event_log
    monkeypatch.setattr(event_log, "append", lambda *a, **k: True)
    import time as _time
    now_ms = int(_time.time() * 1000)
    day_ms = 86400 * 1000
    m = _mem()
    m.record_trade({"seq": "ancient", "coin": "BTC",
                    "executed_at": now_ms - 365 * day_ms})
    assert [t["seq"] for t in m._trades] == ["ancient"]


# ── R10/P3-3: incremental realized-PnL / exit-slip stats ───────────────────

def _close_mem(monkeypatch):
    """A memory instance with flush and the authoritative 'close' event feed
    stubbed, and _day_start_ts pinned to today's UTC midnight (seconds)."""
    from datetime import datetime, timezone
    from hermes_trader import event_log
    monkeypatch.setattr(event_log, "append", lambda *a, **k: True)
    m = _mem()
    m._day_start_ts = int(datetime.now(timezone.utc)
                          .replace(hour=0, minute=0, second=0, microsecond=0)
                          .timestamp())
    return m


def test_daily_realized_pnl_sums_today(monkeypatch):
    # Two same-coin closes today (one win, one loss): the per-coin daily total
    # is their summed realized USD, expressed as % of start-of-day equity.
    m = _close_mem(monkeypatch)
    now_s = int(m._day_start_ts + 3600)  # 01:00 UTC today
    m.record_close({"coin": "BTC", "side": "LONG", "realized_pnl_usd": 1.5,
                    "exit_slip_bps": 10.0, "closed_at": now_s * 1000})
    m.record_close({"coin": "BTC", "side": "SHORT", "realized_pnl_usd": -0.5,
                    "exit_slip_bps": 20.0, "closed_at": (now_s + 60) * 1000})
    # (1.5 - 0.5) = 1.0 USD on 100 start equity = 1.0%
    assert m.coin_daily_realized_pnl_pct("BTC", 100.0) == pytest.approx(1.0)


def test_daily_realized_pnl_excludes_yesterday(monkeypatch):
    # A close stamped before the current UTC day start is not folded into the
    # running total (mirrors the old scan's closed_at >= day_start filter).
    m = _close_mem(monkeypatch)
    yday_s = m._day_start_ts - 3600  # 23:00 UTC yesterday
    m.record_close({"coin": "ETH", "side": "LONG", "realized_pnl_usd": 5.0,
                    "exit_slip_bps": 10.0, "closed_at": yday_s * 1000})
    m.record_close({"coin": "ETH", "side": "LONG", "realized_pnl_usd": 2.0,
                    "exit_slip_bps": 10.0,
                    "closed_at": (m._day_start_ts + 3600) * 1000})
    # Only today's +2.0 USD counts → 2.0% of 100.
    assert m.coin_daily_realized_pnl_pct("ETH", 100.0) == pytest.approx(2.0)


def test_avg_exit_slip_requires_min_samples(monkeypatch):
    # Fewer than min_samples adverse closes → 0.0 (do not widen on noise);
    # at/above the threshold the mean of adverse (positive) slip is returned.
    m = _close_mem(monkeypatch)
    now_s = m._day_start_ts + 60
    m.record_close({"coin": "SOL", "side": "LONG", "realized_pnl_usd": 0.1,
                    "exit_slip_bps": 12.0, "closed_at": now_s * 1000})
    m.record_close({"coin": "SOL", "side": "LONG", "realized_pnl_usd": 0.1,
                    "exit_slip_bps": 18.0, "closed_at": (now_s + 60) * 1000})
    assert m.avg_exit_slip_bps("SOL", min_samples=3) == 0.0
    m.record_close({"coin": "SOL", "side": "LONG", "realized_pnl_usd": 0.1,
                    "exit_slip_bps": 30.0, "closed_at": (now_s + 120) * 1000})
    # mean(12, 18, 30) = 20.0; a favorable (negative) slip is never counted.
    assert m.avg_exit_slip_bps("SOL", min_samples=3) == pytest.approx(20.0)
    m.record_close({"coin": "SOL", "side": "LONG", "realized_pnl_usd": 0.1,
                    "exit_slip_bps": -5.0, "closed_at": (now_s + 180) * 1000})
    assert m.avg_exit_slip_bps("SOL", min_samples=3) == pytest.approx(20.0)


def test_daily_realized_pnl_zero_equity_guard(monkeypatch):
    # start_of_day_equity <= 0 must short-circuit to 0.0 (no division by zero).
    m = _close_mem(monkeypatch)
    m.record_close({"coin": "BTC", "side": "LONG", "realized_pnl_usd": 1.0,
                    "exit_slip_bps": 10.0,
                    "closed_at": (m._day_start_ts + 60) * 1000})
    assert m.coin_daily_realized_pnl_pct("BTC", 0.0) == 0.0
    assert m.coin_daily_realized_pnl_pct("BTC", -10.0) == 0.0
