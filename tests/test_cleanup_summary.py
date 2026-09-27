"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

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


def test_summary_payload_offline_when_no_heartbeat(monkeypatch):
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: [])
    out = dashboard._summary_payload()
    assert out["status"] == "offline"
    assert out["equity"] == 0.0


def test_summary_payload_scanning_and_pnl_pct(monkeypatch):
    """Recent heartbeat → 'scanning'; daily_pnl_pct = pnl / (equity − pnl)."""
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = [
        {"event": "loop_heartbeat", "ts": now_ms, "equity": 260.0,
         "daily_pnl": 10.0, "available": 50.0, "open_positions": 3},
        {"event": "scan", "ts": now_ms, "triggers": 7},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    out = dashboard._summary_payload()
    assert out["status"] == "scanning"
    assert out["daily_pnl"] == 10.0
    # sod = 260 − 10 = 250 → 10/250 = 4.0%
    assert out["daily_pnl_pct"] == 4.0
    assert out["open_positions"] == 3
    assert out["last_scan_triggers"] == 7


def test_summary_scan_count_prefers_perceptions_falls_back_to_triggers(monkeypatch):
    """P0-2b: the HTTP /scan endpoint logs the count as `perceptions` while the
    loop used `triggers`; summary must read `perceptions` (new canonical key)
    but fall back to `triggers` for pre-fix log events."""
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    hb = {"event": "loop_heartbeat", "ts": now_ms, "equity": 100.0,
          "daily_pnl": 0.0, "available": 0.0, "open_positions": 0}
    # Latest scan uses the HTTP-style key: pre-fix this count displayed 0.
    events = [hb, {"event": "scan", "ts": now_ms, "perceptions": 5}]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    assert dashboard._summary_payload()["last_scan_triggers"] == 5
    # Legacy loop-style key still works via the alias.
    events = [hb, {"event": "scan", "ts": now_ms, "triggers": 9}]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    assert dashboard._summary_payload()["last_scan_triggers"] == 9
    # Canonical key wins when both are present.
    events = [hb, {"event": "scan", "ts": now_ms,
                   "perceptions": 3, "triggers": 99}]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    assert dashboard._summary_payload()["last_scan_triggers"] == 3


def test_summary_payload_stale_when_heartbeat_old(monkeypatch):
    from hermes_trader import dashboard
    old_ms = int(dashboard.time.time() * 1000) - 600_000  # 10 min ago
    events = [{"event": "loop_heartbeat", "ts": old_ms, "equity": 100.0,
               "daily_pnl": 0.0}]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    assert dashboard._summary_payload()["status"] == "stale"
