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


def _isolate_dsl_state(monkeypatch, tmp_path):
    """Point DSL persistence at a tmp file and clear the in-memory + load latches."""
    from hermes_trader.agents import dsl_exit
    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    return dsl_exit, state_file


def test_min_order_size_meets_10_dollar_floor():
    """_min_order_size must yield >= $10 notional at the coin's size precision.
    Regression: MEGA ($0.084, integer sizes) — 100 coins is only ~$8.4."""
    from hermes_trader.client.exchange import _min_order_size
    cases = [(0.084334, 0), (1.56, 0), (76000.0, 5), (3.2, 2), (0.0001, 0)]
    for price, sz_dec in cases:
        ms = _min_order_size(price, sz_dec)
        assert ms * price >= 10.0, f"price={price} sz_dec={sz_dec}: ${ms * price:.2f}"
        tick = 10.0 ** (-sz_dec)
        assert abs(round(ms / tick) - ms / tick) < 1e-9  # exact tick multiple
    # the specific regression: MEGA needs more than the old 100-coin cap
    assert _min_order_size(0.084334, 0) > 100


def test_parse_order_result():
    from hermes_trader.client.exchange import _parse_order_result
    filled = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 123}}]}}}
    assert _parse_order_result(filled) == {"ok": True, "order_id": "123"}
    err = {"status": "ok", "response": {"data": {"statuses": [{"error": "bad px"}]}}}
    assert _parse_order_result(err) == {"ok": False, "error": "bad px"}
    resting = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 7}}]}}}
    assert _parse_order_result(resting, accept_resting=True) == {"ok": True, "order_id": "7"}
    assert _parse_order_result("boom")["ok"] is False


def test_parse_order_result_extracts_avg_px_and_total_sz():
    """Realized-PnL computation depends on these fields being threaded through
    from the SDK response — regression guard against the parser dropping them."""
    from hermes_trader.client.exchange import _parse_order_result
    filled = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"oid": 99, "avgPx": "0.5435", "totalSz": "100.0"}}
    ]}}}
    out = _parse_order_result(filled)
    assert out["ok"] is True and out["order_id"] == "99"
    assert out["avg_px"] == 0.5435 and out["total_sz"] == 100.0

    # Garbage avgPx should be tolerated, not raise — order still parses ok.
    garbage = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"oid": 1, "avgPx": "nope"}}
    ]}}}
    g = _parse_order_result(garbage)
    assert g["ok"] is True and "avg_px" not in g


def test_parse_order_result_rejects_empty_statuses():
    """An ok envelope with no statuses must NOT be treated as success.
    Regression guard for the BCH TP 2026-08-21 incident where a trigger order
    was accepted at placement then silently rejected on trigger (minTradeNtl);
    the empty-statuses fallback previously returned {"ok": True}."""
    from hermes_trader.client.exchange import _parse_order_result
    empty = {"status": "ok", "response": {"data": {"statuses": []}}}
    out = _parse_order_result(empty, accept_resting=True)
    assert out["ok"] is False
    assert "no order status" in out["error"]

    weird = {"status": "ok", "response": {"data": {"statuses": [{"unknown": {}}]}}}
    w = _parse_order_result(weird)
    assert w["ok"] is False


def test_close_position_market_computes_realized_pnl_from_fill(monkeypatch, tmp_path):
    """When place_hl_order returns avg_px, the close result carries an exact
    realized PnL (leveraged × spot move from fill, minus taker fees) — this is
    what the dashboard surfaces to match HL's display."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    # Long ARB 10x, entry 0.11684; close fills at 0.10522 → +9.945% spot,
    # +99.45% gross, − (2 × 0.025 × 10 = 0.5%) fees = +98.95% net realized.
    # We register as SHORT here since the screenshot showed ARB SHORT 10x.
    dsl_exit.register_position("ARB", "short", 0.11684, leverage=10)

    # Pin the taker fee so this test's arithmetic is independent of the
    # canonical default (HL tier-0 default moved 2.5bps -> 4.5bps).
    monkeypatch.setenv("HERMES_TAKER_FEE_PCT", "0.025")
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "asset_positions": [{"position": {"coin": "ARB", "szi": "-1000", "entryPx": "0.11684"}}],
    })
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 0.10522)
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda is_buy, size, mid_price, coin, **kw: {
                            "ok": True, "order_id": "999",
                            "avg_px": 0.10522, "total_sz": 1000.0,
                        })
    recorded = []
    monkeypatch.setattr(executor.memory, "record_close", lambda c: recorded.append(c))
    monkeypatch.setattr(executor.memory, "pop_entry_context", lambda coin, side: {})

    res = executor.close_position_market("ARB")
    assert res["ok"] is True
    assert res["side"] == "short"
    assert res["fill_px"] == 0.10522
    assert res["entry_px"] == 0.11684
    assert res["leverage"] == 10
    # Short profits when fill < entry: (0.11684 - 0.10522) / 0.11684 ≈ 9.9452%
    assert abs(res["spot_pct"] - 9.9452) < 0.01
    # Realized = spot × 10 − (0.025 × 2 × 10) = 99.45 − 0.5 = 98.95
    assert abs(res["realized_pnl_pct"] - 98.95) < 0.05
    assert recorded
    assert abs(recorded[0]["gross_pnl_usd"] - 11.62) < 0.01
    assert abs(recorded[0]["fee_usd"] - 0.0584) < 0.001
    assert abs(recorded[0]["realized_pnl_usd"] - 11.5616) < 0.01
    assert "ARB_short" not in dsl_exit._active_positions


def test_close_position_market_without_avgpx_still_records_and_arms(monkeypatch, tmp_path):
    """P0 regression: when the close fill response omits avgPx, settlement must
    STILL run record_close, loss cooldown and the single-coin circuit breaker
    (falling back to mid_price). Previously all three were gated on avgPx, so a
    losing close silently skipped every risk-bookkeeping action."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    # Long TESTX 10x, entry 100; mid 94 → −6% spot loss → −60% ROE net of fees.
    dsl_exit.register_position("TESTX", "long", 100.0, leverage=10)

    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "asset_positions": [{"position": {
            "coin": "TESTX", "szi": "1", "entryPx": "100",
            # Deliberately NO positionValue: forces the mid_price fallback.
        }}],
    })
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 94.0)
    # ok=True but NO avg_px / total_sz — the response shape that skipped
    # bookkeeping before the fix.
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda is_buy, size, mid_price, coin, **kw: {"ok": True, "order_id": "x2"})
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda coin: None)

    recorded = []
    monkeypatch.setattr(executor.memory, "record_close", lambda c: recorded.append(c))
    monkeypatch.setattr(executor.memory, "pop_entry_context", lambda coin, side: {})
    monkeypatch.setattr(executor.memory, "record_loss_outcome", lambda coin, pct: None)
    circuit_calls = []
    monkeypatch.setattr(executor.memory, "set_coin_circuit",
                        lambda coin, until: circuit_calls.append((coin, until)))
    monkeypatch.setattr(executor.memory, "set_loss_cooldown", lambda coin, until: None)
    monkeypatch.setattr(executor.memory, "get_start_of_day_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)

    res = executor.close_position_market("TESTX")
    assert res["ok"] is True
    # mid_price fallback fill: 94 vs entry 100 → −6.0% spot.
    assert res["fill_px"] == 94.0
    assert abs(res["spot_pct"] - (-6.0)) < 0.01
    assert res["spot_pct"] < -5.0
    # record_close ran unconditionally.
    assert recorded and recorded[0]["coin"] == "TESTX"
    # Single-coin breaker (3% spot threshold) armed for the losing coin.
    assert circuit_calls and circuit_calls[0][0] == "TESTX"
    # Tracker deregistered.
    assert "TESTX_long" not in dsl_exit._active_positions


def test_http_session_is_singleton():
    import hermes_trader.client.hl_client as h
    s1 = h._get_session()
    s2 = h._get_session()
    assert s1 is s2
    # adapter pool sized for our fan-out
    adapter = s1.get_adapter("https://api.hyperliquid.xyz")
    assert adapter._pool_maxsize >= 16
