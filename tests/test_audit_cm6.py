"""C-M6 (deep audit 2026-08-28): close_position_market had NO concurrency
protection. It is reachable simultaneously from the trading loop, the
dashboard kill-switch and manual API calls — two concurrent closes both
fetched the same live szi, both placed a reduce-only order and both ran the
settlement block (deregister / record_close / loss streak / breakers),
double-counting realized PnL and inflating the B-F2 consecutive-loss streak.

Fix under test:
  • Per-coin lock (_get_close_lock) serializes the whole fetch→close→settle
    critical section per coin; different coins still close in parallel.
  • Settlement dedupe: a repeat close for the SAME position (same entry_px +
    size) within _CLOSE_DEDUPE_WINDOW_MS returns {"settlement_deduped": True}
    and skips record_close / loss-streak / breaker arming. A genuinely new
    position (different entry or size) has a different signature and settles.
"""

from __future__ import annotations

import threading
import time

import pytest


def _setup(monkeypatch, tmp_path, *, coin="ETH", szi="1.0", entry="100",
           fill_px=94.0, mutable_live=None):
    """Wire a full-fill close environment. `mutable_live`, when given, is a
    dict the test may mutate between calls (e.g. pop the coin after fill to
    simulate the exchange going flat); otherwise the position is always
    reported (used for the dedupe tests)."""
    from hermes_trader.agents import dsl_exit, executor
    from hermes_trader import notify

    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    side = "long" if float(szi) > 0 else "short"
    dsl_exit.register_position(coin, side, float(entry), leverage=10)
    executor._CLOSE_SETTLED_AT.clear()

    monkeypatch.setattr(notify, "send_text", lambda *a, **kw: False)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")

    if mutable_live is None:
        mutable_live = {coin: {"szi": szi, "entryPx": entry}}

    def fake_fetch(u, **kw):
        # Return EVERY coin currently in the mutable live dict so parallel
        # closes of different coins each see their own position.
        return {"asset_positions": [
            {"position": {"coin": c, **p}} for c, p in mutable_live.items()
        ]}

    monkeypatch.setattr(executor, "fetch_account_state", fake_fetch)
    monkeypatch.setattr(executor, "get_hl_price", lambda c: fill_px)

    calls = {"place": 0, "record_close": 0, "loss_outcome": 0,
             "coin_circuit": 0, "cooldown": 0}

    def fake_place(is_buy, size, mid_price, coin, **kw):
        calls["place"] += 1
        return {"ok": True, "order_id": f"oid-{calls['place']}",
                "total_sz": float(size), "avg_px": fill_px}

    monkeypatch.setattr(executor, "place_hl_order", fake_place)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)
    monkeypatch.setattr(executor.memory, "pop_entry_context", lambda *a, **kw: {})

    def _rc(c):
        calls["record_close"] += 1

    def _rlo(c, pct):
        calls["loss_outcome"] += 1

    def _scc(c, until):
        calls["coin_circuit"] += 1

    monkeypatch.setattr(executor.memory, "record_close", _rc)
    monkeypatch.setattr(executor.memory, "record_loss_outcome", _rlo)
    monkeypatch.setattr(executor.memory, "set_coin_circuit", _scc)
    monkeypatch.setattr(executor.memory, "set_loss_cooldown",
                        lambda *a, **kw: calls.__setitem__("cooldown", calls["cooldown"] + 1))
    monkeypatch.setattr(executor.memory, "get_start_of_day_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "global_halt_remaining_min", lambda: 0.0)
    return executor, calls, mutable_live


# ---------------------------------------------------------------------------
# 1) Per-coin lock: concurrent closes of the SAME coin never double-close
# ---------------------------------------------------------------------------

def test_cm6_concurrent_same_coin_closes_once(monkeypatch, tmp_path):
    """Two threads close the same coin at the same time. The exchange goes
    flat as soon as the first reduce-only fills; the second caller must
    re-fetch INSIDE the lock, see already_flat, and place NO second order."""
    executor, calls, live = _setup(monkeypatch, tmp_path,
                                   mutable_live={"ETH": {"szi": "1.0", "entryPx": "100"}})

    def fake_place(is_buy, size, mid_price, coin, **kw):
        # The real exchange: once filled, the position is gone.
        live.pop(coin, None)
        calls["place"] += 1
        return {"ok": True, "order_id": f"oid-{calls['place']}",
                "total_sz": float(size), "avg_px": 94.0}

    monkeypatch.setattr(executor, "place_hl_order", fake_place)

    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(executor.close_position_market("ETH"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    assert len(results) == 2
    assert all(r.get("ok") for r in results)
    # Exactly one order reached the exchange, exactly one settlement ran.
    assert calls["place"] == 1
    assert calls["record_close"] == 1


# ---------------------------------------------------------------------------
# 2) Dedupe: repeat close for the SAME position within the window is suppressed
# ---------------------------------------------------------------------------

def test_cm6_duplicate_settlement_suppressed(monkeypatch, tmp_path):
    """Exchange still (stale-)reports the position on the second call, but we
    settled the identical position (same entry + size) seconds ago → the
    second call skips record_close / loss streak / breaker arming."""
    executor, calls, _ = _setup(monkeypatch, tmp_path)

    r1 = executor.close_position_market("ETH")
    assert r1["ok"] is True
    assert r1.get("settlement_deduped") is not True
    assert calls["record_close"] == 1
    # entry 100 → fill 94 = -6% spot, >= 3% single-coin breaker → armed once.
    assert calls["loss_outcome"] == 1
    assert calls["coin_circuit"] == 1

    r2 = executor.close_position_market("ETH")
    assert r2["ok"] is True
    assert r2.get("settlement_deduped") is True
    # A second reduce-only was attempted (exchange still reported the szi),
    # but NO settlement side effects ran a second time.
    assert calls["place"] == 2
    assert calls["record_close"] == 1
    assert calls["loss_outcome"] == 1
    assert calls["coin_circuit"] == 1


# ---------------------------------------------------------------------------
# 3) A genuinely NEW position (different entry/size) still settles
# ---------------------------------------------------------------------------

def test_cm6_new_position_within_window_still_settles(monkeypatch, tmp_path):
    from hermes_trader.agents import dsl_exit
    executor, calls, live = _setup(monkeypatch, tmp_path)

    executor.close_position_market("ETH")
    assert calls["record_close"] == 1

    # Re-entered at a different price/size → different dedupe signature.
    live["ETH"] = {"szi": "2.0", "entryPx": "200"}
    dsl_exit.register_position("ETH", "long", 200.0, leverage=10)

    r = executor.close_position_market("ETH")
    assert r.get("settlement_deduped") is not True
    assert calls["record_close"] == 2


# ---------------------------------------------------------------------------
# 4) Dedupe expires after the window
# ---------------------------------------------------------------------------

def test_cm6_dedupe_expires_after_window(monkeypatch, tmp_path):
    executor, calls, _ = _setup(monkeypatch, tmp_path)

    executor.close_position_market("ETH")
    assert calls["record_close"] == 1

    # Age the settlement signature beyond the dedupe window.
    prev = executor._CLOSE_SETTLED_AT["ETH"]
    old_ts = int(time.time() * 1000) - executor._CLOSE_DEDUPE_WINDOW_MS - 1
    executor._CLOSE_SETTLED_AT["ETH"] = (old_ts, prev[1], prev[2])

    r = executor.close_position_market("ETH")
    assert r.get("settlement_deduped") is not True
    assert calls["record_close"] == 2


# ---------------------------------------------------------------------------
# 5) Lock granularity: same coin → same lock; different coins → different
# ---------------------------------------------------------------------------

def test_cm6_lock_is_per_coin():
    from hermes_trader.agents import executor
    a1 = executor._get_close_lock("ETH")
    a2 = executor._get_close_lock("ETH")
    b = executor._get_close_lock("BTC")
    assert a1 is a2
    assert a1 is not b


# ---------------------------------------------------------------------------
# 6) Different coins close in parallel (no cross-coin blocking)
# ---------------------------------------------------------------------------

def test_cm6_different_coins_close_in_parallel(monkeypatch, tmp_path):
    from hermes_trader.agents import executor
    executor, calls, live = _setup(monkeypatch, tmp_path, coin="ETH")
    # Second coin lives in the same shared live-dict.
    live["BTC"] = {"szi": "0.1", "entryPx": "50000"}

    monkeypatch.setattr(executor, "get_hl_price", lambda c: 94.0)

    def slow_place(is_buy, size, mid_price, coin, **kw):
        time.sleep(0.3)  # network latency simulation
        calls["place"] += 1
        return {"ok": True, "order_id": f"oid-{coin}",
                "total_sz": float(size), "avg_px": 94.0}

    monkeypatch.setattr(executor, "place_hl_order", slow_place)

    t0 = time.time()
    t1 = threading.Thread(target=executor.close_position_market, args=("ETH",))
    t2 = threading.Thread(target=executor.close_position_market, args=("BTC",))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)
    elapsed = time.time() - t0

    # Serial would be ~0.6s; parallel per-coin locks finish in ~0.3s.
    assert elapsed < 0.5
    assert calls["place"] == 2
    assert calls["record_close"] == 2
