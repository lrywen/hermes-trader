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


def test_operator_action_forks_to_events_feed(tmp_path, monkeypatch):
    """F22: operator_action (manual close / kill / mode / config via web or
    terminal) is in the fork whitelist and lands in events.jsonl so the audit
    trail survives a restart."""
    assert "operator_action" in event_log._FORKABLE_EVENTS

    sess_file = tmp_path / "session.jsonl"
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess_file))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))

    session_log.append({
        "event": "operator_action",
        "action": "close",
        "via": "web",
        "coin": "BTC",
        "result": {"ok": True, "fill_px": 50000.0, "leverage": 10},
    })

    events = _read_events(str(ev_file))
    ops = [e for e in events if e["event"] == "operator_action"]
    assert len(ops) == 1
    assert ops[0]["payload"]["action"] == "close"
    assert ops[0]["payload"]["via"] == "web"
    assert ops[0]["payload"]["coin"] == "BTC"
    assert ops[0]["timestamp"].endswith("Z")


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


def test_risk_gate_block_durably_recorded(tmp_path, monkeypatch):
    """P1-5: a gate block must write a structured `risk_gate` record to
    events.jsonl (coin/side/reasons/gates/ctx), not just a debug log line."""
    from hermes_trader.agents import executor
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))

    executor._record_risk_gate_block(
        {"coin": "BTC", "side": "long", "verdict": "LONG",
         "confidence": 0.72, "composite_score": 55, "trace_id": "tr-1"},
        {"block_reasons": ["cooldown active", "max concurrent positions"],
         "results": {
             "cooldown": {"pass": False, "reason": "cooldown active"},
             "max_concurrent": {"pass": False,
                                "reason": "max concurrent positions"},
             "trend_gate": {"pass": True, "reason": "ok"},
         }},
    )
    events = _read_events(str(ev_file))
    blocks = [e for e in events if e["event"] == "risk_gate"]
    assert len(blocks) == 1
    p = blocks[0]["payload"]
    assert p["coin"] == "BTC"
    assert p["side"] == "long"
    assert blocks[0]["trace_id"] == "tr-1"
    assert "cooldown active" in p["block_reasons"]
    # Only the vetoing gates are carried, each mapped to its reason.
    assert set(p["gates"].keys()) == {"cooldown", "max_concurrent"}
    assert p["gates"]["cooldown"] == "cooldown active"


def test_dashboard_redact_scrubs_secrets():
    """F17: _redact must mask Authorization/Bearer/api_key fields and a raw
    key string interpolated into a message, before it reaches a response."""
    from hermes_trader.dashboard import _redact
    out = _redact({
        "Authorization": "Bearer sk-secret-123",
        "openrouter_api_key": "sk-secret-123",
        "coin": "BTC",
        "nested": {"token": "abc", "ok": 1},
    })
    assert out["Authorization"] == "<redacted>"
    assert out["openrouter_api_key"] == "<redacted>"
    assert out["coin"] == "BTC"
    assert out["nested"]["token"] == "<redacted>"
    assert out["nested"]["ok"] == 1

    msg = _redact("request failed: Bearer sk-secret-123 not authorized")
    assert "sk-secret-123" not in msg
    assert "Bearer <redacted>" in msg
