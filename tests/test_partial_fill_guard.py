"""P0-5 regression: reduce-only close that PARTIALLY fills must NOT deregister
the DSL tracker. Previously `close_position_market` called `deregister_position`
immediately after `place_hl_order(ok=True)`, so any partial fill leaked the
residual inventory: local said "flat" but the exchange still held szi → the
position kept running PnL and funding, and the next cycle's "no position"
close was a no-op. This test pins the new behaviour:

  • partial fill (>5% gap OR >0.001 coin absolute) → return {"partial": True, ...},
    do NOT deregister, do NOT cancel SL bracket, do NOT call record_close,
    do NOT arm loss cooldown, do NOT arm coin-circuit. The local tracker is
    intentionally kept so the next scan tick re-detects the residual and
    either auto-mops via a follow-up reduce-only close or escalates to
    human review.
  • full fill (within tolerance) → original happy path (deregister +
    settlement) still runs unchanged.
  • first close fails entirely (ok=False) → no follow-up attempt, no
    partial flag, no notify.
"""

from __future__ import annotations

import pytest


def _isolate(monkeypatch, tmp_path):
    """Mirror test_cleanup._isolate_dsl_state — kept private to this file
    because the helper is not exported from there."""
    from hermes_trader.agents import dsl_exit, executor
    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    # Provide a small in-memory no-op for get_hl_price so the function under
    # test can compute mid_price without hitting the network.
    monkeypatch.setattr(executor, "get_hl_price", lambda coin: 100.0)
    # Silence notify: tests do not need real Feishu pings.
    from hermes_trader import notify
    monkeypatch.setattr(notify, "send_text", lambda *a, **kw: False)
    return dsl_exit, executor


def _patch_account(monkeypatch, executor, *, coin: str, szi: str, entry: str = "100"):
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "asset_positions": [{"position": {"coin": coin, "szi": szi, "entryPx": entry}}],
    })


# ---------------------------------------------------------------------------
# 1) Partial fill → does NOT deregister, marks partial, alerts, no settlement
# ---------------------------------------------------------------------------

def test_partial_fill_keeps_tracker_and_marks_partial(monkeypatch, tmp_path):
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 100.0, leverage=5)

    _patch_account(monkeypatch, executor, coin="ETH", szi="1.0", entry="100")

    # First close: only fills 0.4 of 1.0. Follow-up closes the remaining 0.6.
    place_calls = []

    def fake_place(is_buy, size, mid_price, coin, **kw):
        place_calls.append({"size": size, "coin": coin, "is_buy": is_buy, "reduce_only": kw.get("reduce_only")})
        # First call: 0.4 filled. Second call (follow-up of 0.6): 0.6 filled.
        if len(place_calls) == 1:
            return {"ok": True, "order_id": "oid-1", "total_sz": 0.4, "avg_px": 99.0}
        return {"ok": True, "order_id": "oid-2", "total_sz": size, "avg_px": 99.0}

    monkeypatch.setattr(executor, "place_hl_order", fake_place)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: pytest.fail("SL cancel must NOT run on partial"))
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: pytest.fail("record_close must NOT run on partial"),
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("ETH")

    # Return shape must mark partial + carry sizes.
    assert res["ok"] is True
    assert res.get("partial") is True
    assert res["requested_sz"] == 1.0
    assert res["filled_sz"] == 0.4
    assert res["follow_filled_sz"] == 0.6
    assert res["residual_sz"] == 0.0
    # Tracker MUST still be present (residual still on exchange, even if
    # follow-up fully closed it; we re-verify next tick before deregister).
    assert "ETH_long" in dsl_exit._active_positions
    # Follow-up reduce_only at the same direction (close long → is_buy=False).
    assert len(place_calls) == 2
    assert place_calls[1]["size"] == pytest.approx(0.6)
    assert place_calls[1]["is_buy"] is False
    assert place_calls[1]["reduce_only"] is True


def test_partial_fill_with_residual_alerts_and_keeps_tracker(monkeypatch, tmp_path):
    """Both attempts partial → residual_sz > 0 → high-priority notify + still no deregister."""
    from hermes_trader import notify
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("BTC", "long", 100.0, leverage=10)
    _patch_account(monkeypatch, executor, coin="BTC", szi="0.5", entry="100")

    notified = []
    monkeypatch.setattr(notify, "send_text", lambda msg, **kw: notified.append((msg, kw)) or False)

    # First close fills only 0.1, follow-up fills 0.2 → residual 0.2.
    n = {"i": 0}
    def fake_place(is_buy, size, mid_price, coin, **kw):
        n["i"] += 1
        if n["i"] == 1:
            return {"ok": True, "total_sz": 0.1, "avg_px": 99.0}
        return {"ok": True, "total_sz": 0.2, "avg_px": 99.0}
    monkeypatch.setattr(executor, "place_hl_order", fake_place)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: pytest.fail("record_close must NOT run on partial"),
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("BTC")
    assert res["partial"] is True
    assert res["requested_sz"] == 0.5
    assert res["filled_sz"] == 0.1
    assert res["follow_filled_sz"] == 0.2
    assert res["residual_sz"] == pytest.approx(0.2)
    # Tracker must remain: residual alive on the exchange.
    assert "BTC_long" in dsl_exit._active_positions
    # High-priority alert fired.
    assert notified, "partial-fill alert must be sent"
    assert notified[0][1].get("category") == "risk"
    assert "部分成交" in notified[0][0] or "PARTIAL" in notified[0][0].upper() or "0.2" in notified[0][0]


def test_partial_fill_follow_up_exception_does_not_crash(monkeypatch, tmp_path):
    """If the follow-up place_hl_order raises, we still return cleanly with
    residual_sz set and tracker kept — never let a partial close blow up the
    trading loop."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("SOL", "long", 100.0, leverage=5)
    _patch_account(monkeypatch, executor, coin="SOL", szi="2.0", entry="100")

    n = {"i": 0}
    def fake_place(is_buy, size, mid_price, coin, **kw):
        n["i"] += 1
        if n["i"] == 1:
            return {"ok": True, "total_sz": 1.0, "avg_px": 99.0}
        raise RuntimeError("network down")
    monkeypatch.setattr(executor, "place_hl_order", fake_place)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: pytest.fail("record_close must NOT run on partial"),
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("SOL")
    assert res["ok"] is True
    assert res["partial"] is True
    # follow_filled=0 because the second call raised; residual = 2.0 - 1.0 - 0.0.
    assert res["follow_filled_sz"] == 0.0
    assert res["residual_sz"] == pytest.approx(1.0)
    assert "SOL_long" in dsl_exit._active_positions


def test_partial_fill_total_sz_missing_defaults_to_filled(monkeypatch, tmp_path):
    """Defensive: if total_sz is absent or 0 in the response, treat as fully
    filled (gap=0, ≤ tolerance) so we don't false-positive partial on every
    response that omits total_sz. This is the conservative choice — the
    settlement path is the well-tested one; a missing total_sz shouldn't
    silently strand a tracker."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("DOGE", "long", 100.0, leverage=3)
    _patch_account(monkeypatch, executor, coin="DOGE", szi="10", entry="100")
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **kw: {"ok": True, "order_id": "x", "avg_px": 99.0})  # no total_sz
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: None,
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("DOGE")
    assert res["ok"] is True
    assert "partial" not in res
    assert "DOGE_long" not in dsl_exit._active_positions


# ---------------------------------------------------------------------------
# 2) Full fill within tolerance → original happy path
# ---------------------------------------------------------------------------

def test_full_fill_deregisters_and_runs_settlement(monkeypatch, tmp_path):
    """Equal fill (1.0 of 1.0) and tiny dust (gap < 0.001 coin AND < 5%) both
    fall into the original deregister + settlement path."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("AVAX", "long", 100.0, leverage=5)
    _patch_account(monkeypatch, executor, coin="AVAX", szi="1.0", entry="100")

    # Exact full fill.
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **kw: {"ok": True, "order_id": "x", "total_sz": 1.0, "avg_px": 99.0})
    cancelled = []
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: cancelled.append(c))

    # Real-ish memory mock that accepts **kwargs so downstream code paths
    # (record_close, set_coin_circuit, set_loss_cooldown, etc.) can call with
    # the right arg shapes without us having to mirror every signature.
    recorded = []
    class _M:
        def record_close(self, c):
            recorded.append(c)
        def pop_entry_context(self, *a, **kw): return {}
        def record_loss_outcome(self, *a, **kw): return None
        def set_coin_circuit(self, *a, **kw): return None
        def set_loss_cooldown(self, *a, **kw): return None
        def get_start_of_day_equity(self): return 0.0
        def get_daily_pnl(self): return 0.0
    monkeypatch.setattr(executor, "memory", _M())

    res = executor.close_position_market("AVAX")
    assert res["ok"] is True
    assert "partial" not in res
    assert "AVAX_long" not in dsl_exit._active_positions
    assert cancelled == ["AVAX"]
    assert recorded, "record_close must run on full fill"


def test_tiny_dust_within_absolute_tolerance_is_full_fill(monkeypatch, tmp_path):
    """A gap of 0.0005 (below 0.001 absolute floor) is treated as full fill."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("LINK", "long", 100.0, leverage=5)
    _patch_account(monkeypatch, executor, coin="LINK", szi="1.0", entry="100")
    # 0.9995 filled of 1.0 → gap 0.0005 (< 0.001 floor).
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **kw: {"ok": True, "order_id": "x", "total_sz": 0.9995, "avg_px": 99.0})
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: None,
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("LINK")
    assert res["ok"] is True
    assert "partial" not in res
    assert "LINK_long" not in dsl_exit._active_positions


def test_tiny_relative_dust_within_5pct_is_full_fill(monkeypatch, tmp_path):
    """For larger positions, a 3% gap (below 5% relative) is treated as full."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("WIF", "long", 100.0, leverage=5)
    _patch_account(monkeypatch, executor, coin="WIF", szi="100.0", entry="100")
    # 97 of 100 filled → gap 3 (< 5% of 100 = 5).
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **kw: {"ok": True, "order_id": "x", "total_sz": 97.0, "avg_px": 99.0})
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: None,
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("WIF")
    assert res["ok"] is True
    assert "partial" not in res
    assert "WIF_long" not in dsl_exit._active_positions


# ---------------------------------------------------------------------------
# 3) Hard-fail paths (ok=False) → unchanged behaviour
# ---------------------------------------------------------------------------

def test_first_close_fails_no_follow_up(monkeypatch, tmp_path):
    """place_hl_order returning ok=False must not trigger the follow-up
    partial-fill path (gap is irrelevant when the first attempt was a hard fail)."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("PEPE", "long", 100.0, leverage=5)
    _patch_account(monkeypatch, executor, coin="PEPE", szi="1000", entry="100")

    calls = {"n": 0}
    def fake_place(is_buy, size, mid_price, coin, **kw):
        calls["n"] += 1
        return {"ok": False, "error": "margin_short"}
    monkeypatch.setattr(executor, "place_hl_order", fake_place)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: pytest.fail("must not run on hard fail"),
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("PEPE")
    assert res["ok"] is False
    assert "partial" not in res
    assert calls["n"] == 1, "follow-up must NOT be attempted on hard fail"
    # Tracker stays: position still live on exchange.
    assert "PEPE_long" in dsl_exit._active_positions


def test_short_close_partial_uses_correct_direction(monkeypatch, tmp_path):
    """Closing a short: is_buy must be True (buy to cover), and the follow-up
    reduce-only must also be buy-to-cover."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, executor = _isolate(monkeypatch, tmp_path)
    dsl_exit.register_position("ARB", "short", 0.11684, leverage=10)
    _patch_account(monkeypatch, executor, coin="ARB", szi="-1000", entry="0.11684")

    place_calls = []
    n = {"i": 0}
    def fake_place(is_buy, size, mid_price, coin, **kw):
        place_calls.append({"is_buy": is_buy, "size": size, "reduce_only": kw.get("reduce_only")})
        n["i"] += 1
        if n["i"] == 1:
            return {"ok": True, "total_sz": 400.0, "avg_px": 0.105}
        return {"ok": True, "total_sz": size, "avg_px": 0.105}
    monkeypatch.setattr(executor, "place_hl_order", fake_place)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: pytest.fail("must not run on partial"))
    monkeypatch.setattr(executor, "memory", type("M", (), {
        "record_close": lambda c: pytest.fail("must not run on partial"),
        "pop_entry_context": lambda *a, **kw: {},
        "record_loss_outcome": lambda *a, **kw: None,
        "set_coin_circuit": lambda *a, **kw: None,
        "set_loss_cooldown": lambda *a, **kw: None,
        "get_start_of_day_equity": lambda: 0.0,
        "get_daily_pnl": lambda: 0.0,
    })())

    res = executor.close_position_market("ARB")
    assert res["partial"] is True
    assert res["requested_sz"] == 1000.0
    assert res["filled_sz"] == 400.0
    # Closing a short = buy to cover.
    assert place_calls[0]["is_buy"] is True
    assert place_calls[1]["is_buy"] is True
    assert place_calls[1]["size"] == pytest.approx(600.0)
    assert "ARB_short" in dsl_exit._active_positions
