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


def _hb_equities(now_ms, equities):
    return [{"event": "loop_heartbeat", "ts": now_ms - (len(equities) - i) * 60_000,
             "equity": float(eq)} for i, eq in enumerate(equities)]


def test_equity_curve_payload_filters_by_range_and_zero(monkeypatch):
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = [
        {"event": "loop_heartbeat", "ts": now_ms - 7200_000, "equity": 200.0},  # 2h old
        {"event": "loop_heartbeat", "ts": now_ms - 60_000, "equity": 240.0},    # recent
        {"event": "loop_heartbeat", "ts": now_ms, "equity": 0.0},               # zero-skip
        {"event": "scan", "ts": now_ms, "equity": 999.0},                       # wrong event
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    out = dashboard._equity_curve_payload(range_s=3600)  # last hour only
    assert [p["equity"] for p in out] == [240.0]


def test_equity_curve_cashflow_neutral_adjustment(monkeypatch):
    """P0-1: a deposit bumps raw equity but must NOT read as trading profit.

    equity_adj subtracts the cumulative external flow; the window-relative
    return_pct is cash-flow-neutral, so a $100 deposit with zero trading leaves
    the adjusted level (and return) unchanged while raw equity rises.
    """
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = [
        {"event": "loop_heartbeat", "ts": now_ms - 120_000, "equity": 200.0,
         "cum_contrib": 0.0},
        {"event": "loop_heartbeat", "ts": now_ms - 60_000, "equity": 300.0,
         "cum_contrib": 100.0},   # $100 deposited, zero trading
        {"event": "loop_heartbeat", "ts": now_ms, "equity": 330.0,
         "cum_contrib": 100.0},   # +$30 of genuine trading gain
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    out = dashboard._equity_curve_payload(range_s=3600)
    # Raw equity rises across the deposit...
    assert [p["equity"] for p in out] == [200.0, 300.0, 330.0]
    # ...but adjusted equity strips the deposit: 200, 200, 230.
    assert [p["equity_adj"] for p in out] == [200.0, 200.0, 230.0]
    # Cash-neutral return counts only the genuine +$30 on a $200 base = 15%.
    assert out[0]["return_pct"] == 0.0
    assert out[1]["return_pct"] == 0.0
    assert out[2]["return_pct"] == 15.0


def test_equity_curve_legacy_heartbeat_without_cum_contrib(monkeypatch):
    """P0-1: pre-upgrade heartbeats lack cum_contrib → degrade to no adjustment
    (equity_adj == raw equity) rather than crashing."""
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = [
        {"event": "loop_heartbeat", "ts": now_ms - 60_000, "equity": 240.0},
        {"event": "loop_heartbeat", "ts": now_ms, "equity": 250.0},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    out = dashboard._equity_curve_payload(range_s=3600)
    assert [p["equity_adj"] for p in out] == [240.0, 250.0]
    # return_pct falls back to raw-equity change: (250-240)/240 ≈ 4.1667%.
    assert abs(out[-1]["return_pct"] - (10.0 / 240.0 * 100)) < 1e-3


def test_equity_curve_keeps_and_flags_degraded_dip(monkeypatch):
    """F1: a far-below-median point is FLAGGED, not silently dropped — a genuine
    flash-crash must stay visible on the curve."""
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = _hb_equities(now_ms, [200.0, 200.0, 200.0, 88.0, 205.0])
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    out = dashboard._equity_curve_payload(range_s=3600)
    # The dip point is retained (no longer dropped)...
    assert [p["equity"] for p in out] == [200.0, 200.0, 200.0, 88.0, 205.0]
    flags = [p["flag"] for p in out]
    assert flags == ["ok", "ok", "ok", "degraded", "ok"]


def test_equity_curve_gradual_decline_not_flagged(monkeypatch):
    """A sustained gradual move (real drawdown/growth) never trips the dip
    detector — each step stays above the ratio of the trailing median."""
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = _hb_equities(now_ms, [100.0, 95.0, 90.0, 85.0, 80.0])
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    out = dashboard._equity_curve_payload(range_s=3600)
    assert all(p["flag"] == "ok" for p in out)


def test_equity_curve_dip_ratio_env_override(monkeypatch):
    """HERMES-equivalent tuning: lowering the ratio widens what counts as ok."""
    from hermes_trader import dashboard
    now_ms = int(dashboard.time.time() * 1000)
    events = _hb_equities(now_ms, [200.0, 200.0, 120.0])
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    # Default ratio 0.7: 120 < 0.7*200=140 → degraded.
    monkeypatch.setattr(dashboard, "_EQUITY_DIP_RATIO", 0.7)
    assert dashboard._equity_curve_payload(3600)[-1]["flag"] == "degraded"
    # Ratio 0.5: 120 >= 0.5*200=100 → ok.
    monkeypatch.setattr(dashboard, "_EQUITY_DIP_RATIO", 0.5)
    assert dashboard._equity_curve_payload(3600)[-1]["flag"] == "ok"
