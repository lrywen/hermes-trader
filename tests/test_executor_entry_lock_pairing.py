"""P1-1 step ③ prerequisite — characterization tests for the entry lock and
in-flight marker acquire→release pairing around the irreversible order path
in ``executor.maybe_execute``.

The single 1,775-line maybe_execute takes the cross-process entry flock and
claims the two in-process in-flight sets by hand, and releases them at many
separate return points (there is deliberately NO single try/finally wrapping
place_hl_order — see test_audit_batch_b_cross_process). Refactoring that tail
into phases is high-risk precisely because a missed release silently wedges a
coin: a leaked in-flight marker makes the coin permanently
``coin_order_in_flight``/``already_executed``; a leaked flock makes every
later entry return ``entry_lock_busy``.

These tests LOCK THE CURRENT BEHAVIOUR (characterization, not ideal design):
for every exit branch after the claim/flock is taken, both markers must be
gone and the flock must be released (``_fd is None``). They are the safety net
the upcoming phase split must keep green.
"""
from __future__ import annotations

import pytest

from hermes_trader.agents import executor
from hermes_trader.client.lock import EntryOrderLock

_AID = "lockpair"
_COIN = "LKTEST"


def _base_analysis():
    return {
        "id": _AID, "coin": _COIN, "verdict": "LONG",
        "side": "long", "confidence": 0.9, "composite_score": 80.0,
    }


@pytest.fixture
def live_to_lock(monkeypatch, tmp_path):
    """Wire maybe_execute in LIVE mode all the way to the claim + flock.

    Returns a dict of call-recorders the individual tests override to force a
    specific post-lock branch. The cross-process flock is replaced with a
    fresh one rooted in tmp_path so tests never contend on the real sidecar.
    """
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": False,
        "min_available_margin_pct": 0.0, "max_atr_pct": 15.0,
        "max_spread_pct": 1.0,
    })
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "equity": 1000.0, "available": 1000.0,
        "dex_equity": {"": 1000.0}, "dex_available": {"": 1000.0},
        "total_ntl": 0.0, "asset_positions": [],
    })
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 100.0)
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 10)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 1.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "eval_all_gates",
                        lambda ctx, cfg, lt, **kw: {"blocked": False, "results": {}})
    monkeypatch.setattr(executor, "get_orderbook_spread", lambda c: {
        "ok": True, "spread_pct": 0.01,
        "bid_depth_1pct_usd": 1e9, "ask_depth_1pct_usd": 1e9,
        "best_bid": 99.99, "best_ask": 100.01})
    monkeypatch.setattr(executor, "set_leverage", lambda c, lev: {"ok": True})
    monkeypatch.setattr(executor, "_check_liquidation_buffer",
                        lambda *a, **k: {"ok": True})

    # Fresh, tmp-rooted, non-blocking flock + guaranteed-released markers.
    fresh_lock = EntryOrderLock(name="entry-order-ctest", lock_dir=str(tmp_path))
    assert fresh_lock._fd is None
    monkeypatch.setattr(executor, "_ENTRY_LOCK", fresh_lock)
    with executor._EXEC_LOCK:
        executor._IN_FLIGHT_ANALYSES.discard(_AID)
        executor._IN_FLIGHT_COINS.discard(_COIN)

    rec = {"lock": fresh_lock}
    return rec


def _assert_fully_released(rec):
    """The lock-pairing invariant: flock released AND both markers cleared."""
    assert rec["lock"]._fd is None, "cross-process entry flock was leaked"
    with executor._EXEC_LOCK:
        assert _AID not in executor._IN_FLIGHT_ANALYSES, "analysis marker leaked"
        assert _COIN not in executor._IN_FLIGHT_COINS, "coin marker leaked"


def test_entry_lock_busy_releases_claim_without_taking_flock(live_to_lock, monkeypatch):
    # Another holder already owns the flock: acquire() fails. The in-process
    # claim taken immediately before must be rolled back (no marker leak).
    monkeypatch.setattr(live_to_lock["lock"], "acquire", lambda: False)
    res = executor.maybe_execute(_base_analysis())
    assert res["reason"] == "entry_lock_busy"
    _assert_fully_released(live_to_lock)


def test_liq_buffer_block_releases_lock_and_markers(live_to_lock, monkeypatch):
    monkeypatch.setattr(executor, "_check_liquidation_buffer",
                        lambda *a, **k: {"ok": False, "error": "too close"})
    res = executor.maybe_execute(_base_analysis())
    assert res["reason"].startswith("liq_buffer_blocked:")
    _assert_fully_released(live_to_lock)


def test_pre_place_existing_position_releases_lock_and_markers(live_to_lock, monkeypatch):
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "equity": 1000.0, "available": 1000.0,
        "dex_equity": {"": 1000.0}, "dex_available": {"": 1000.0},
        "total_ntl": 0.0,
        "asset_positions": [{"position": {"coin": _COIN, "szi": "1.0"}}],
    })
    res = executor.maybe_execute(_base_analysis())
    assert res["reason"] == "position_already_open_pre_place"
    _assert_fully_released(live_to_lock)


def test_pre_place_recheck_failure_releases_lock_and_markers(live_to_lock, monkeypatch):
    calls = {"n": 0}

    def _state(u, **kw):
        calls["n"] += 1
        # First read (top of pipeline) succeeds; the pre-place recheck raises
        # -> fail-closed skip.
        if calls["n"] >= 2:
            raise RuntimeError("stale read")
        return {"equity": 1000.0, "available": 1000.0,
                "dex_equity": {"": 1000.0}, "dex_available": {"": 1000.0},
                "total_ntl": 0.0, "asset_positions": []}

    monkeypatch.setattr(executor, "fetch_account_state", _state)
    res = executor.maybe_execute(_base_analysis())
    assert res["reason"] == "pre_place_recheck_failed"
    _assert_fully_released(live_to_lock)


def test_price_divergence_block_releases_lock_and_markers(live_to_lock, monkeypatch):
    import hermes_trader.client.price_crosscheck as pcc
    monkeypatch.setattr(pcc, "crosscheck_price",
                        lambda coin, mid: {"ok": False, "checked": True,
                                           "action": "block", "reason": "x"})
    res = executor.maybe_execute(_base_analysis())
    assert res["reason"].startswith("price_divergence_blocked:")
    _assert_fully_released(live_to_lock)


def test_definite_order_failure_releases_lock_and_markers(live_to_lock, monkeypatch):
    # A definite HL rejection makes _reconcile_unknown_order_result return a
    # non-None result; maybe_execute releases the flock on that branch and
    # returns the failure dict.
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **k: {"ok": False, "error": "rejected",
                                         "error_code": "reject"})
    res = executor.maybe_execute(_base_analysis())
    assert res["executed"] is False
    assert str(res["reason"]).startswith("order_failed")
    _assert_fully_released(live_to_lock)


def test_repeated_post_lock_aborts_leave_no_accumulating_state(live_to_lock, monkeypatch):
    """Driving the same post-lock abort branch on repeated ticks must not
    accumulate residue (each abort fully releases both markers + the flock),
    otherwise a coin would wedge itself after the first skipped tick."""
    monkeypatch.setattr(executor, "_check_liquidation_buffer",
                        lambda *a, **k: {"ok": False, "error": "x"})
    for _ in range(2):
        res = executor.maybe_execute(_base_analysis())
        assert res["reason"].startswith("liq_buffer_blocked:")
        _assert_fully_released(live_to_lock)
