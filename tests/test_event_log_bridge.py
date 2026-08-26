"""Event-log bridge: order/close/session events reach events.jsonl so memory
can rebuild from the authoritative feed (the PURR record-loss fix, 2026-08-22).
"""
from __future__ import annotations

import json
import os

from hermes_trader import event_log, session_log
from hermes_trader.agents.memory import AgentMemory


def _read_events(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def test_record_trade_emits_order_event(tmp_path, monkeypatch):
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    m = AgentMemory()
    m._initialized = True
    m.record_trade({"coin": "PURR", "side": "long",
                    "entry_px": 0.1, "order_id": "o1"})
    events = _read_events(str(ev_file))
    orders = [e for e in events if e["event"] == "order"]
    assert len(orders) == 1
    assert orders[0]["payload"]["coin"] == "PURR"
    assert orders[0]["payload"]["order_id"] == "o1"
    assert orders[0]["timestamp"]  # ISO-8601 present


def test_record_close_emits_close_event(tmp_path, monkeypatch):
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    m = AgentMemory()
    m._initialized = True
    m.record_close({"coin": "PURR", "side": "long",
                    "entry_px": 0.1, "exit_px": 0.09,
                    "realized_pnl_usd": -0.5})
    events = _read_events(str(ev_file))
    closes = [e for e in events if e["event"] == "close"]
    assert len(closes) == 1
    assert closes[0]["payload"]["exit_px"] == 0.09


def test_rebuild_recovers_trade_and_close(tmp_path, monkeypatch):
    """A fresh AgentMemory must reconstruct trades/closes from events.jsonl
    even when the JSON cache is empty/wiped — the exact PURR failure."""
    ev_file = tmp_path / "events.jsonl"
    mem_file = tmp_path / "mem.json"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    import hermes_trader.agents.memory as memory_mod
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(ev_file))
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_file))

    # Simulate the live process having written events.jsonl.
    event_log.EVENTS_FILE = str(ev_file)
    writer = AgentMemory()
    writer._initialized = True
    writer.record_trade({"coin": "BTC", "side": "long", "order_id": "btc1"})
    writer.record_close({"coin": "BTC", "side": "long",
                         "exit_px": 50000, "realized_pnl_usd": 12.0})

    # A brand-new singleton (fresh process) with no JSON cache must rebuild.
    fresh = AgentMemory()
    fresh._initialized = False
    fresh.load()
    assert len(fresh.get_all_trades()) == 1
    assert len(fresh.get_closes()) == 1
    assert fresh.get_closes()[0]["coin"] == "BTC"


def test_session_log_forks_outcome_events(tmp_path, monkeypatch):
    sess_file = tmp_path / "session.jsonl"
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess_file))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))

    session_log.append({"event": "execute", "coin": "ETH", "side": "long"})
    session_log.append({"event": "scan", "triggers": 0})  # not forked

    events = _read_events(str(ev_file))
    names = [e["event"] for e in events]
    assert "execute" in names
    assert "scan" not in names
    # Session log still got both (heartbeat preserved).
    sess = _read_events(str(sess_file))
    assert len(sess) == 2


def test_fork_from_session_schema(tmp_path, monkeypatch):
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    ok = event_log.fork_from_session({
        "ts": 1700000000000, "event": "dsl_exit",
        "trace_id": "t1", "coin": "SOL", "side": "long",
    })
    assert ok is True
    events = _read_events(str(ev_file))
    assert events[0]["event"] == "dsl_exit"
    assert events[0]["trace_id"] == "t1"
    assert events[0]["payload"]["coin"] == "SOL"
    assert events[0]["timestamp"].endswith("Z")
