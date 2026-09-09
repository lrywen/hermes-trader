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

from hermes_trader.agents.config_store import (
    read_agent_config,
    write_agent_config,
)
from hermes_trader.agents.memory import AgentMemory


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


# ── CS-G: direction-differentiated slip / hold series ──────────────────────

def _cs_close(m, coin, side, slip=None, hold_min=None, off=0):
    """A today-stamped close row for CS-G stats (off = seconds past 01:00)."""
    row = {"coin": coin, "side": side,
           "closed_at": (m._day_start_ts + 3600 + off) * 1000}
    if slip is not None:
        row["exit_slip_bps"] = slip
    if hold_min is not None:
        row["hold_minutes"] = hold_min
    m.record_close(row)


def test_side_slip_prefers_coin_side_then_coin_shared(monkeypatch):
    # 3 short closes for BTC feed coin_side. A single long close leaves the
    # long coin_side series under min_samples, but the shared coin series
    # (all four closes qualify) does — so the long read falls through to
    # coin_shared with mean(4, 6, 8, 12) = 7.5.
    m = _close_mem(monkeypatch)
    for i, v in enumerate((4.0, 6.0, 8.0)):
        _cs_close(m, "ZBTC", "short", slip=v, off=i * 60)
    bps, src = m.avg_exit_slip_bps_side("ZBTC", "short", min_samples=3)
    assert src == "coin_side"
    assert bps == pytest.approx(6.0)
    _cs_close(m, "ZBTC", "long", slip=12.0, off=1000)
    bps, src = m.avg_exit_slip_bps_side("ZBTC", "long", min_samples=3)
    assert src == "coin_shared"
    assert bps == pytest.approx(7.5)


def test_side_slip_falls_back_to_global_side_pool(monkeypatch):
    # No BTC data at all: three OTHER coins' same-side (short) means pool
    # together; opposite-side (long) rows must not enter the pool.
    m = _close_mem(monkeypatch)
    for i, coin in enumerate("ZABC"):
        for j, v in enumerate((5.0, 7.0)):
            _cs_close(m, coin, "short", slip=v, off=i * 300 + j * 60)
    for i, coin in enumerate("ZABC"):
        for j, v in enumerate((50.0, 70.0)):
            _cs_close(m, coin, "long", slip=v, off=10_000 + i * 300 + j * 60)
    bps, src = m.avg_exit_slip_bps_side("ZUNKNOWN", "short", min_samples=3)
    assert src == "global_side"
    assert bps == pytest.approx(6.0)


def test_side_slip_cold_default_and_bad_side(monkeypatch):
    # Empty memory (and an unseen coin): conservative 2.0 bps default, never
    # zero-width. An unrecognized side argument normalizes to long.
    m = _close_mem(monkeypatch)
    bps, src = m.avg_exit_slip_bps_side("ZUNKNOWN", "short", min_samples=3)
    assert src == "default"
    assert bps == 2.0
    bps, src = m.avg_exit_slip_bps_side("ZUNKNOWN", "weird", min_samples=3)
    assert src == "default"
    assert bps == 2.0


def test_side_hold_converts_minutes_to_hours_coin_side(monkeypatch):
    # hold_minutes is stored/returned in HOURS.
    m = _close_mem(monkeypatch)
    for i, mins in enumerate((120.0, 240.0, 480.0)):  # 2h, 4h, 8h
        _cs_close(m, "ZBTC", "long", hold_min=mins, off=i * 60)
    hours, src = m.avg_hold_hours_side("ZBTC", "long", min_samples=3)
    assert src == "coin_side"
    assert hours == pytest.approx((2.0 + 4.0 + 8.0) / 3.0)


def test_side_hold_global_pool_and_default(monkeypatch):
    # Three other coins' long holds pool into global_side; an unseen coin with
    # no pool falls back to the 8.0h conservative default.
    m = _close_mem(monkeypatch)
    for i, coin in enumerate("ZABC"):
        for j, mins in enumerate((120.0, 240.0)):  # 2h, 4h
            _cs_close(m, coin, "long", hold_min=mins, off=i * 300 + j * 60)
    hours, src = m.avg_hold_hours_side("ZUNKNOWN", "long", min_samples=3)
    assert src == "global_side"
    assert hours == pytest.approx(3.0)
    hours, src = m.avg_hold_hours_side("ZUNKNOWN", "short", min_samples=3)
    assert src == "default"
    assert hours == 8.0


def test_close_stats_rebuild_replays_side_series(monkeypatch):
    # A rebuild (restart/hydration path) must reconstruct _slip_side /
    # _hold_side from the _closes rows with identical reader results.
    m = _close_mem(monkeypatch)
    for i, v in enumerate((4.0, 6.0, 8.0)):
        _cs_close(m, "ZBTC", "short", slip=v, hold_min=120.0 + i * 60,
                  off=i * 60)
    before = (m.avg_exit_slip_bps_side("ZBTC", "short", min_samples=3),
              m.avg_hold_hours_side("ZBTC", "short", min_samples=3))
    m._rebuild_close_stats_nolock()
    after = (m.avg_exit_slip_bps_side("ZBTC", "short", min_samples=3),
             m.avg_hold_hours_side("ZBTC", "short", min_samples=3))
    assert before == after
    assert after[0][1] == "coin_side"
    assert after[0][0] == pytest.approx(6.0)
    assert after[1][0] == pytest.approx((2.0 + 3.0 + 4.0) / 3.0)


def test_side_series_evicts_in_lockstep_with_closes(monkeypatch):
    # Pin the closes cap to 3: appending a 4th/5th short close evicts the
    # oldest _closes row and its per-side slip/hold deque heads together.
    from hermes_trader.agents import memory as memory_mod
    real_limits = memory_mod._memory_limits

    def tiny_limits():
        d = dict(real_limits())
        d["closes"] = 3
        return d

    monkeypatch.setattr(memory_mod, "_memory_limits", tiny_limits)
    m = _close_mem(monkeypatch)
    _cs_close(m, "ZBTC", "short", slip=4.0, hold_min=60.0, off=0)
    _cs_close(m, "ZBTC", "short", slip=6.0, hold_min=120.0, off=60)
    _cs_close(m, "ZBTC", "short", slip=8.0, hold_min=180.0, off=120)
    _cs_close(m, "ZBTC", "short", slip=10.0, hold_min=240.0, off=180)
    assert len(m._closes) == 3
    # Deques hold the surviving three rows: 6 / 8 / 10 bps → 8.0;
    # 2 / 3 / 4 hours → 3.0h.
    assert len(m._slip_side[("ZBTC", "short")]) == 3
    assert len(m._hold_side[("ZBTC", "short")]) == 3
    bps, src = m.avg_exit_slip_bps_side("ZBTC", "short", min_samples=3)
    assert src == "coin_side"
    assert bps == pytest.approx(8.0)
    hours, src = m.avg_hold_hours_side("ZBTC", "short", min_samples=3)
    assert src == "coin_side"
    assert hours == pytest.approx(3.0)


def test_side_series_normalizes_uppercase_legacy_rows(monkeypatch):
    # Legacy disk rows written with side="SHORT"/"LONG" must fold into the
    # same lowercase-keyed side series, and uppercase reader args must match.
    m = _close_mem(monkeypatch)
    for i, v in enumerate((4.0, 6.0, 8.0)):
        _cs_close(m, "ZBTC", "SHORT", slip=v, off=i * 60)
    bps, src = m.avg_exit_slip_bps_side("ZBTC", "SHORT", min_samples=3)
    assert src == "coin_side"
    assert bps == pytest.approx(6.0)
    bps, src = m.avg_exit_slip_bps_side("ZBTC", "short", min_samples=3)
    assert bps == pytest.approx(6.0)
