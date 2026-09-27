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


def test_closed_trades_payload_dsl_realized_fill(monkeypatch):
    """A dsl_exit with realized fill data reports the exact PnL, newest-first."""
    from hermes_trader import dashboard
    events = [
        {"event": "execute", "coin": "BTC", "side": "long", "ts": 1},
        {"event": "dsl_exit", "coin": "BTC", "ts": 2, "leverage": 10,
         "realized_pnl_pct": 8.0, "realized_spot_pct": 0.8, "reason": "trail"},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    # F13: _closed_trades_payload also merges events.jsonl (close events);
    # other tests write real close rows into the conftest tmp events file,
    # so isolate the reconcile source here.
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    out = dashboard._closed_trades_payload()
    assert len(out) == 1
    row = out[0]
    assert row["coin"] == "BTC" and row["side"] == "long"
    assert row["pnl_source"] == "fill"
    assert row["pnl_pct"] == 8.0
    assert row["leverage_estimated"] is False


def test_closed_trades_payload_estimates_leverage_and_side(monkeypatch):
    """Old dsl_exit lacking side/leverage walks back to the execute event for
    side and estimates leverage from config × HL cap."""
    from hermes_trader import dashboard
    events = [
        {"event": "execute", "coin": "ETH", "side": "short", "ts": 1},
        {"event": "dsl_exit", "coin": "ETH", "ts": 2,
         "unrealized_pct": -1.0, "reason": "stop"},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    # F13: isolate the events.jsonl reconcile source (see test above).
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    monkeypatch.setattr(dashboard, "cfg_get", lambda key, config=None: 20 if key == "leverage" else None)
    monkeypatch.setattr(dashboard, "_load_max_lev_table", lambda: {"ETH": 25})
    out = dashboard._closed_trades_payload()
    row = out[0]
    assert row["side"] == "short"
    assert row["leverage"] == 20  # min(cfg 20, HL cap 25)
    assert row["leverage_estimated"] is True
    assert row["pnl_source"] == "estimated"


def test_closed_trades_payload_respects_limit(monkeypatch):
    from hermes_trader import dashboard
    events = []
    for i in range(5):
        events.append({"event": "execute", "coin": "BTC", "side": "long", "ts": i})
        events.append({"event": "dsl_exit", "coin": "BTC", "ts": i + 100,
                       "leverage": 5, "realized_pnl_pct": 1.0, "realized_spot_pct": 0.2})
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    assert len(dashboard._closed_trades_payload(limit=3)) == 3


def test_closed_trades_dedup_requires_cross_source_and_side(monkeypatch):
    """F2: dedup merges ONLY cross-source mirrors of one fill. Same-source rows
    are distinct trades even within the window; a cross-source pair of OPPOSITE
    sides (long close vs short close) must never collapse."""
    from hermes_trader import dashboard
    now = int(dashboard.time.time() * 1000)
    events = [
        # two distinct DSL closes 800ms apart — same source, same coin/side
        {"event": "dsl_exit", "coin": "SOL", "side": "long", "ts": now,
         "leverage": 5, "realized_pnl_pct": 1.0, "realized_spot_pct": 0.2},
        {"event": "dsl_exit", "coin": "SOL", "side": "long", "ts": now + 800,
         "leverage": 5, "realized_pnl_pct": 0.5, "realized_spot_pct": 0.1},
        # a manual-close mirror of the FIRST fill (same ts window, same side) → merges
        {"event": "close_position", "coin": "SOL", "side": "long", "ts": now + 200,
         "leverage": 5, "ok": True},
        # an external close on the OPPOSITE side within the window → must stay
        {"event": "external_close_recorded", "coin": "SOL", "side": "short",
         "ts": now + 400, "leverage": 5, "spot_pct": -0.3},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    rows = dashboard._closed_trades_payload(limit=20)
    # 2 DSL longs (one absorbs the manual mirror) + 1 external short = 3 rows
    assert len(rows) == 3
    sources = sorted(r["source"] for r in rows)
    assert sources == ["dsl", "dsl", "external"]


def test_closed_trades_external_usd_reconstruction(monkeypatch):
    """NEAR exchange-trigger fill (oid 542364812239): measured net $0.4257 on
    entry 2.6322 → exit 2.6702 at 10x. Notional must reconstruct near the real
    $30.0071 (the old net-as-gross inversion reported ~14.44% ROE / ~0 fee;
    the round-trip-fee-doubled variant reported $31.69)."""
    from hermes_trader import dashboard
    events = [
        {"event": "external_close_recorded", "coin": "NEAR", "side": "long",
         "ts": 1, "leverage": 10,
         "entry_px": 2.6322, "exit_px": 2.6702,
         "spot_pct": 1.4437, "realized_pnl_usd": 0.4257, "oid": 542364812239},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    row = dashboard._closed_trades_payload(limit=20)[0]
    assert row["exit_mechanism"] == "exchange_trigger"
    assert row["close_oid"] == 542364812239
    assert row["pnl_source"] == "fill_usd"
    assert row["pnl_usd"] == 0.4257
    assert row["notional_usd"] is not None
    assert abs(row["notional_usd"] - 30.0071) < 0.7   # reconstruct, not fabricate
    # net ROE ~13.9% and a real round-trip fee ~0.5% (never 14.44% / ~0)
    assert 13.5 < row["pnl_pct"] < 14.2
    assert row["fees_pct"] > 0.3
    assert row["fee_usd"] > 0.01


def test_closed_trades_dedup_by_oid_beyond_time_window(monkeypatch):
    """Mirrors sharing a close_oid merge even >5s apart (post-restart backfill
    can trail by minutes); the richer reconcile row wins and inherits the DSL
    strategy reason/hold/regime via cross-source enrichment."""
    from hermes_trader import dashboard
    now = int(dashboard.time.time() * 1000)
    events = [
        {"event": "dsl_exit", "coin": "NEAR", "side": "long", "ts": now,
         "leverage": 10, "reason": "external_close_backfill",
         "exit_reason": "exchange_trigger",
         "detail": "backfill oid=542364812239", "hold_min": 16.2,
         "entry_regime": "neutral", "executed": True,
         "realized_pnl_pct": 14.1867, "realized_spot_pct": 1.48765,
         "fees_pct": 0.6898},
    ]
    outcome = [
        {"event": "close", "timestamp": "2026-09-11T14:27:00+00:00",
         "payload": {"coin": "NEAR", "side": "long", "leverage": 10,
                     "closed_at": now + 15600,
                     "entry_px": 2.6322, "exit_px": 2.6702,
                     "spot_pct": 1.48765, "realized_pnl_pct": 14.1867,
                     "realized_pnl_usd": 0.4257, "gross_pnl_usd": 0.4463,
                     "fee_usd": 0.0207, "notional_usd": 30.0071,
                     "hold_minutes": 16.2, "regime_at_entry": "neutral",
                     "close_source": "exchange_trigger",
                     "close_oid": 542364812239}},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: outcome)
    rows = dashboard._closed_trades_payload(limit=20)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "reconcile"          # richest wins despite ts gap
    assert row["close_oid"] == 542364812239
    assert row["pnl_usd"] == 0.4257              # measured money fields
    assert row["notional_usd"] == 30.0071
    assert row["hold_minutes"] == 16.2
    assert row["entry_regime"] == "neutral"
    assert row["exit_mechanism"] == "exchange_trigger"
    assert row["reason"] == "exchange_trigger"


def test_closed_trades_outcome_dsl_exit_restores_strategy_reason(monkeypatch):
    """A DSL market close lands in events.jsonl as TWO records: a `close`
    (measured dollars, no reason/oid — the executor's record_close path) and a
    `dsl_exit` mirror carrying the strategy reason (ZEC: max_loss). The dsl_exit
    mirror must be parsed and merged cross-source so the close shows the real
    reason instead of None/"—"."""
    from hermes_trader import dashboard
    closed_at = 1788945938803
    outcome = [
        {"event": "close", "timestamp": "2026-09-09T09:25:39Z",
         "payload": {"coin": "ZEC", "side": "long", "leverage": 10,
                     "closed_at": closed_at, "entry_px": 1252.4, "exit_px": 1242.0,
                     "spot_pct": -0.8304, "realized_pnl_pct": -8.8041,
                     "realized_pnl_usd": -0.2205, "gross_pnl_usd": -0.208,
                     "fee_usd": 0.0125, "notional_usd": 25.048,
                     "hold_minutes": 9.2, "regime_at_entry": "up"}},
        {"event": "dsl_exit", "timestamp": "2026-09-09T09:25:39Z",
         "payload": {"coin": "ZEC", "side": "long", "leverage": 10,
                     "reason": "max_loss (0.83% spot / 8.3% ROE >= cap; held 12.7s)",
                     "exit_reason": "max_loss", "entry_regime": "up",
                     "close_source": "dsl", "hold_min": 9.17,
                     "executed": True, "detail": "540119066100",
                     "fill_px": 1242.0, "entry_px": 1252.4,
                     "realized_spot_pct": -0.8304,
                     "realized_pnl_pct": -8.8041, "fees_pct": 0.5}},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: [])
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: outcome)
    rows = dashboard._closed_trades_payload(limit=20)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "reconcile"
    assert row["reason"] == "max_loss"
    assert row["exit_mechanism"] == "dsl_market"
    assert row["pnl_usd"] == -0.2205
    assert row["fee_usd"] == 0.0125
    assert row["entry_regime"] == "up"


def test_closed_trades_manual_backfill_close_maps_to_exchange_trigger(monkeypatch):
    """An ancient close backfilled with close_source=
    exchange_trigger_manual_backfill has no dsl_exit twin (PURR 2026-08-22);
    it must still surface as an exchange-triggered close, not a reasonless
    dsl_market row."""
    from hermes_trader import dashboard
    closed_at = 1787394168426
    outcome = [
        {"event": "close", "timestamp": "2026-08-22T03:42:48Z",
         "payload": {"coin": "PURR", "side": "long", "leverage": 5,
                     "close_source": "exchange_trigger_manual_backfill",
                     "closed_at": closed_at, "entry_px": 0.2050, "exit_px": 0.2100,
                     "close_oid": 523031401396,
                     "spot_pct": 2.439, "realized_pnl_pct": 11.97,
                     "realized_pnl_usd": 0.7182, "gross_pnl_usd": 0.7317,
                     "fee_usd": 0.0135, "notional_usd": 30.0,
                     "hold_minutes": 31.0, "regime_at_entry": "neutral"}},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: [])
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: outcome)
    rows = dashboard._closed_trades_payload(limit=20)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "exchange_trigger"
    assert row["exit_mechanism"] == "exchange_trigger"
    assert row["close_oid"] == 523031401396


def test_closed_trades_same_source_modeled_vs_measured_close_merge(monkeypatch):
    """DOT: the executor's DSL-path record_close (a reconcile `close` with NO
    oid, modeled fees) and the exchange-trigger backfill (a reconcile `close`
    WITH the real oid and measured fees) land 985ms apart, and the dsl_exit
    mirror ties them together. All three must collapse into ONE row; the
    measured oid row wins (real dollars/fees/mechanism), not the modeled one."""
    from hermes_trader import dashboard
    t_model = 1788884842797
    t_fill = 1788884841812
    outcome = [
        {"event": "close", "timestamp": "2026-09-08T16:27:23Z",
         "payload": {"coin": "DOT", "side": "long", "leverage": 10,
                     "closed_at": t_model, "entry_px": 1.172, "exit_px": 1.1846,
                     "spot_pct": 1.02509, "realized_pnl_pct": 10.2509,
                     "realized_pnl_usd": 0.3061, "gross_pnl_usd": 0.3063,
                     "fee_usd": 0.0149, "notional_usd": 29.886,
                     "hold_minutes": 53.8, "regime_at_entry": "down"}},
        {"event": "close", "timestamp": "2026-09-08T16:27:25Z",
         "payload": {"coin": "DOT", "side": "long", "leverage": 10,
                     "closed_at": t_fill, "entry_px": 1.172, "exit_px": 1.1846,
                     "spot_pct": 1.04999, "realized_pnl_pct": 10.4999,
                     "realized_pnl_usd": 0.3138, "gross_pnl_usd": 0.3138,
                     "fee_usd": 0.0205, "notional_usd": 29.886,
                     "hold_minutes": 53.8, "regime_at_entry": "down",
                     "close_source": "exchange_trigger",
                     "close_oid": 539488396836}},
        {"event": "dsl_exit", "timestamp": "2026-09-08T16:27:26Z",
         "payload": {"coin": "DOT", "side": "long", "leverage": 10,
                     "reason": "external_close_backfill",
                     "exit_reason": "exchange_trigger", "entry_regime": "down",
                     "close_source": "exchange_trigger", "hold_min": 53.8,
                     "executed": True, "detail": "backfill oid=539488396836",
                     "fill_px": 1.1846, "entry_px": 1.172,
                     "realized_spot_pct": 1.04999,
                     "realized_pnl_pct": 10.4999, "fees_pct": 0.005}},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: [])
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: outcome)
    rows = dashboard._closed_trades_payload(limit=20)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "reconcile"
    assert row["close_oid"] == 539488396836
    assert row["exit_mechanism"] == "exchange_trigger"
    assert row["reason"] == "exchange_trigger"
    # measured row's money fields win over the modeled checkpoint
    assert row["pnl_usd"] == 0.3138
    assert row["fee_usd"] == 0.0205
    assert row["pnl_pct"] == 10.4999


def test_closed_trades_same_source_distinct_closes_stay_separate(monkeypatch):
    """Two genuine DSL closes for the same coin+side within the window (both
    oid-less `close` records) must NOT be chained into one by the same-source
    reconcile merge carve-out."""
    from hermes_trader import dashboard
    t = 1788884842797
    def _close(closed_at, pnl):
        return {"event": "close", "timestamp": "2026-09-08T16:27:23Z",
                "payload": {"coin": "PURR", "side": "long", "leverage": 10,
                            "closed_at": closed_at, "spot_pct": pnl / 10.0,
                            "realized_pnl_pct": pnl, "realized_pnl_usd": 0.01,
                            "gross_pnl_usd": 0.01, "fee_usd": 0.001,
                            "notional_usd": 10.0, "hold_minutes": 1.0}}
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: [])
    monkeypatch.setattr(dashboard, "_read_outcome_lines",
                        lambda: [_close(t, -1.0), _close(t + 2000, 2.0)])
    rows = dashboard._closed_trades_payload(limit=20)
    assert len(rows) == 2


def test_trades_payload_pairs_open_and_close(monkeypatch):
    """Unified timeline lists the FILLED execute as kind=open, pairs it with
    the close (pair_id/open_ts/hold), and keeps unmatched opens visible."""
    from hermes_trader import dashboard
    t0 = 1_000_000
    events = [
        {"event": "execute", "coin": "BTC", "side": "long", "ts": t0,
         "executed": True, "detail": "oid-open-1", "size_usd": 25.0,
         "entry_px": 100.0, "stop_px": 95.0, "tp_px": 110.0,
         "regime": "trend_up"},
        {"event": "dsl_exit", "coin": "BTC", "side": "long", "ts": t0 + 600_000,
         "leverage": 10, "reason": "trailing_stop hit",
         "realized_pnl_pct": 2.0, "realized_spot_pct": 0.25},
        # an unfilled attempt must NOT become an open row
        {"event": "execute", "coin": "ETH", "side": "short", "ts": t0 + 1,
         "executed": False, "blocked_by": ["market_regime"], "size_usd": None},
        # a still-open position: visible with no pair
        {"event": "execute", "coin": "SOL", "side": "long", "ts": t0 + 900_000,
         "executed": True, "detail": "oid-open-2", "size_usd": 30.0,
         "entry_px": 150.0, "regime": "neutral"},
    ]
    monkeypatch.setattr(dashboard, "_read_log_lines", lambda: events)
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    tl = dashboard._trades_payload(limit=20)
    kinds = {(r["coin"], r["kind"]) for r in tl}
    assert ("BTC", "open") in kinds and ("BTC", "close") in kinds
    assert ("SOL", "open") in kinds
    assert ("ETH", "open") not in kinds
    by_key = {(r["coin"], r["kind"]): r for r in tl}
    op = by_key[("BTC", "open")]
    cl = by_key[("BTC", "close")]
    assert op["pair_id"] and op["pair_id"] == cl["pair_id"]
    assert cl["open_ts"] == t0
    assert cl["hold_minutes"] == 10.0
    assert op["regime"] == "trend_up"
    assert op["notional_usd"] == 25.0
    # reason canonicalised from the raw floor-style string
    assert cl["reason"] == "trailing_stop"
    assert cl["exit_mechanism"] == "dsl_market"
    # newest-first: SOL open (t0+900k) leads, BTC close before BTC open
    assert tl[0]["coin"] == "SOL" and tl[0]["pair_id"] is None
    ts_desc = [r["ts"] for r in tl]
    assert ts_desc == sorted(ts_desc, reverse=True)
