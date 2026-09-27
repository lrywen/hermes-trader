"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import json
import math
import pathlib
import time

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def test_latest_trade_ts_by_coin_keeps_newest():
    """The pre-research cooldown map must keep the NEWEST trade per coin, not
    the oldest — the NEAR double-trade bug that burned LLM tokens every cycle."""
    from hermes_trader.agents.memory import AgentMemory
    m = AgentMemory()
    # Chronological: NEAR traded 67min ago, then again 5min ago.
    m._trades = [
        {"coin": "NEAR", "executed_at": 1_000_000},   # older
        {"coin": "BTC",  "executed_at": 1_500_000},
        {"coin": "NEAR", "executed_at": 9_000_000},   # newer — must win
        {"coin": "SOL"},                              # no executed_at → skipped
    ]
    out = m.latest_trade_ts_by_coin(20)
    assert out["NEAR"] == 9_000_000  # newest, not 1_000_000
    assert out["BTC"] == 1_500_000
    assert "SOL" not in out


def test_memory_flush_writes_atomically(monkeypatch, tmp_path):
    """A live memory flush must not expose half-written JSON to backtests."""
    from hermes_trader.agents import memory as memory_mod

    path = tmp_path / "agent-memory.json"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(path))
    m = memory_mod.AgentMemory()
    m._initialized = True
    m._trades = [{"id": "t1", "coin": "BTC", "size_usd": 10}]
    # P1-6: flush() skips a clean store — a direct field mutation must arm
    # the dirty flag (as every real mutator does) for the write to happen.
    m._dirty = True

    m.flush()

    assert json.loads(path.read_text())["trades"][0]["coin"] == "BTC"
    assert not pathlib.Path(str(path) + ".tmp").exists()


def test_memory_load_tolerates_corrupt_rows(monkeypatch, tmp_path):
    """P0-2a: a single malformed entry in any persisted memory field must NOT
    abort hydration — pre-fix the direct cooldown subscript / dict
    comprehensions raised and were swallowed by the broad except, losing ALL
    history. Valid entries survive; bad ones degrade to defaults."""
    import hermes_trader.event_log as event_log
    from hermes_trader.agents import memory as memory_mod
    mem_path = tmp_path / "agent-memory.json"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_path))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(mem_path) + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(tmp_path / "events.jsonl"))
    future = int(time.time() * 1000) + 3_600_000
    mem_path.write_text(json.dumps({
        "equity": "not-a-number",
        "dayStartTs": "junk",
        "cooldowns": [
            {"coin": "BTC", "expires": future},   # valid
            {"expires": future},                  # missing coin → skip
            {"coin": "ETH", "expires": "soonish"},# bad expires → skip
            "scandal",                            # non-dict row → skip
        ],
        "coinCircuit": {"DOGE": future, "XRP": "garbage"},
        "consecutiveLosses": {"SOL": 2, "AVAX": {}},
        "perceptions": {"p": 1},                  # dict, not list → []
        "trades": [{"id": "t1", "coin": "BTC"}],  # valid
        "equityTrail": [(1700000000.0, 900.0), "bad", [1700000001.0, 850.0]],
        "openPositions": "flatten-me",
    }))

    m = memory_mod.AgentMemory()
    m.load()  # must not raise; hydrates everything recoverable

    assert m._cooldowns.get("BTC") == future
    assert "ETH" not in m._cooldowns
    assert m._coin_circuit.get("DOGE") == future
    assert "XRP" not in m._coin_circuit
    assert m._consecutive_losses.get("SOL") == 2
    assert "AVAX" not in m._consecutive_losses
    assert m._trades[0]["coin"] == "BTC"
    assert m._perceptions == []
    assert m._open_positions == []
    assert len(m._equity_trail) == 2
    assert m._equity == 0.0
    assert m._day_start_ts == 0


def test_open_position_coins_filters_zero_size():
    """Held-coin set drives the cooldown exemption so the AI can still CLOSE
    open positions; zero-size / malformed entries are excluded."""
    from hermes_trader.agents.memory import AgentMemory
    m = AgentMemory()
    m.update_open_positions([
        {"position": {"coin": "NEAR", "szi": "12.0"}},
        {"position": {"coin": "BTC", "szi": "0"}},     # flat → excluded
        {"position": {"coin": "xyz:SNDK", "szi": "-3"}},  # short still counts
        {"position": {"szi": "5"}},                    # no coin → excluded
        "garbage",                                     # non-dict → excluded
    ])
    assert m.open_position_coins() == {"NEAR", "xyz:SNDK"}


def test_memory_record_and_read():
    from hermes_trader.agents.memory import AgentMemory
    m = AgentMemory()
    m.record_trade({"id": "t1", "coin": "BTC", "size_usd": 10})
    m.record_analysis({"id": "a1", "coin": "BTC"})
    assert m.get_recent_trades()[-1]["id"] == "t1"
    assert m.get_analysis_by_id("a1")["coin"] == "BTC"


def test_last_event_returns_newest_match():
    from hermes_trader.dashboard import _last_event
    events = [
        {"event": "scan", "id": 1},
        {"event": "execute", "id": 2},
        {"event": "scan", "id": 3},
    ]
    assert _last_event(events, "scan")["id"] == 3
    assert _last_event(events, "nope") is None
