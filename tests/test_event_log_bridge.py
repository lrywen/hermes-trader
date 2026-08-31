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


# ── O-10 (supplemental audit 2026-08-30): tamper-evident hash chain ──────

def test_hash_chain_links_and_verifies_clean(tmp_path, monkeypatch):
    """A sequence of appended records forms a valid hash chain: seq increments
    from 1, each prev_hash links to the prior hash, and verify_chain() is ok."""
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    for i in range(5):
        assert event_log.append("execute", {"coin": "BTC", "i": i}, trace_id=f"t{i}")

    rows = _read_events(str(ev_file))
    assert len(rows) == 5
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["prev_hash"] == ""
    for i in range(1, 5):
        assert rows[i]["prev_hash"] == rows[i - 1]["hash"]
        assert rows[i]["hash"] and len(rows[i]["hash"]) == 64  # sha256 hexdigest

    res = event_log.verify_chain(str(ev_file))
    assert res["ok"] is True, res["errors"]
    assert res["chained_records"] == 5
    assert res["last_seq"] == 5


def test_hash_chain_detects_tampered_payload(tmp_path, monkeypatch):
    """Rewriting one record's payload must break its hash -> hash_mismatch."""
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    for i in range(4):
        event_log.append("execute", {"coin": "BTC", "px": 100 + i})

    # Tamper with the third line's payload (simulate post-hoc editing).
    lines = ev_file.read_text().splitlines()
    rec = json.loads(lines[2])
    rec["payload"]["px"] = 9999
    lines[2] = json.dumps(rec)
    ev_file.write_text("\n".join(lines) + "\n")

    res = event_log.verify_chain(str(ev_file))
    assert res["ok"] is False
    reasons = [e["reason"] for e in res["errors"]]
    assert "hash_mismatch" in reasons
    bad = [e for e in res["errors"] if e["reason"] == "hash_mismatch"][0]
    assert bad["line"] == 3


def test_hash_chain_detects_deleted_line(tmp_path, monkeypatch):
    """Deleting an interior record breaks the link for the following line."""
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    for i in range(5):
        event_log.append("execute", {"coin": "ETH", "i": i})

    lines = ev_file.read_text().splitlines()
    # Drop record seq=3 (the third line).
    del lines[2]
    ev_file.write_text("\n".join(lines) + "\n")

    res = event_log.verify_chain(str(ev_file))
    assert res["ok"] is False
    reasons = [e["reason"] for e in res["errors"]]
    # The record that used to be 4th now sits at seq=4 while the predecessor
    # is seq=2 -> seq_gap; its prev_hash also points at the deleted hash.
    assert "seq_gap" in reasons or "prev_hash_mismatch" in reasons


def test_hash_chain_tolerates_legacy_records(tmp_path, monkeypatch):
    """Pre-chain records (no hash fields) are counted as legacy and tolerated;
    chained records after them anchor a fresh genesis run and still verify."""
    ev_file = tmp_path / "events.jsonl"
    # Write two legacy lines in the old 4-field schema, then append chained ones.
    legacy = [
        {"event": "order", "trace_id": "", "timestamp": "2026-08-20T00:00:00Z",
         "payload": {"coin": "OLD"}},
        {"event": "close", "trace_id": "", "timestamp": "2026-08-20T00:01:00Z",
         "payload": {"coin": "OLD"}},
    ]
    ev_file.write_text("".join(json.dumps(r) + "\n" for r in legacy))

    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    assert event_log.append("execute", {"coin": "NEW"})

    res = event_log.verify_chain(str(ev_file))
    assert res["ok"] is True, res["errors"]
    assert res["legacy_records"] == 2
    assert res["chained_records"] == 1  # fresh run restarts at seq=1


def test_hash_chain_anchors_across_rotation(tmp_path, monkeypatch):
    """After rotation the first record of the new generation links to the tail
    of the rotated file via the anchor sidecar (no genesis reset of the
    running chain head), and both generations verify individually."""
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    monkeypatch.setattr(event_log, "_MAX_BACKUPS", 3)

    # First append: file empty, no rotation; anchor written with its hash.
    event_log.append("execute", {"coin": "BTC", "i": 1})
    one_line = ev_file.read_text()
    # Cap at exactly one line's size so the NEXT append sees size >= cap and
    # rotates (rotation triggers when the existing file meets the cap).
    monkeypatch.setattr(event_log, "_MAX_EVENTS_BYTES", len(one_line))

    # Second append: the prior line meets the cap, so the file is rotated
    # to .1 (its tail anchored) and a fresh genesis file starts — but
    # _chain_tail() reads the anchor so the new record's seq continues at 2.
    event_log.append("execute", {"coin": "BTC", "i": 2})
    # Restore a large cap so the append into the fresh file does not rotate
    # again and keeps the .1 generation stable.
    monkeypatch.setattr(event_log, "_MAX_EVENTS_BYTES", 50 * 1024 * 1024)
    # Third append into the fresh file: no rotation, chains onto seq 2.
    event_log.append("execute", {"coin": "BTC", "i": 3})

    backup = tmp_path / "events.jsonl.1"
    assert backup.exists(), "rotation should have moved the first generation"
    b_rows = _read_events(str(backup))
    a_rows = _read_events(str(ev_file))
    assert len(b_rows) == 1 and len(a_rows) == 2
    # The running chain continues across the rotation via the anchor sidecar:
    # the first record of the new generation carries seq=2 and links its
    # prev_hash to the rotated tail, then seq=3 chains onto it normally.
    assert a_rows[0]["seq"] == 2
    assert a_rows[0]["prev_hash"] == b_rows[0]["hash"]
    assert a_rows[1]["seq"] == 3
    assert a_rows[1]["prev_hash"] == a_rows[0]["hash"]

    # Verifying the active path replays the retained generations oldest-first
    # as ONE continuous chain (backup tail -> active head), so the rotated
    # link must hold and all 3 records verify with no gap/mismatch.
    res = event_log.verify_chain(str(ev_file))
    assert res["ok"] is True, res["errors"]
    assert res["chained_records"] == 3
    assert res["last_seq"] == 3

    # The rotated backup's single record is a self-consistent genesis record
    # (seq=1, empty prev_hash): recompute its hash and confirm it matches.
    b = b_rows[0]
    assert b["seq"] == 1
    assert b["prev_hash"] == ""
    assert event_log._record_hash(b) == b["hash"]
