"""Audit batch G-P1 (2026-09-10) guard tests.

Three P1 hardening fixes for the ENTRY path, all aimed at fund safety:

  G-P1-2 + D-1 — /api/agent/execute forces a read-only refresh of the disk
  risk state before maybe_execute (the server process's memory singleton is
  otherwise only hydrated at startup), and the refresh whitelist now includes
  dailyPnl / peakDailyPnl so the daily-loss kill switch is live in the API
  process.

  G-P1-1 — a manual entry whose order envelope is LOST (response_unknown:
  408/5xx/timeout after submit) is reconciled three ways against userFills:
    filled     -> backfill the result and continue into the bracket path;
    not_filled -> safe 400 (retryable, no order exists);
    unknown    -> fail-closed 503 + LOUD alert (operator must verify).

  G-P1-3 — a non-blocking cross-process flock (client/lock.EntryOrderLock) is
  shared by the API server and the autonomous executor so their check-then-
  place windows cannot interleave across processes; the manual pre-place
  position re-read is changed from fail-open to fail-closed (503).

This module is self-contained (tests/ has no __init__.py).
"""
import inspect
import json
import os
import time
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(_REPO_ROOT, "hermes_trader", "server.py")
EXECUTOR_PY = os.path.join(_REPO_ROOT, "hermes_trader", "agents", "executor.py")
MEMORY_PY = os.path.join(_REPO_ROOT, "hermes_trader", "agents", "memory.py")
LOCK_PY = os.path.join(_REPO_ROOT, "hermes_trader", "client", "lock.py")

_OP_TOKEN = "test-op-secret-gp1"


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


# ──────────────────────────────────────────────────────────────────────────
# G-P1-3: EntryOrderLock primitive
# ──────────────────────────────────────────────────────────────────────────

def test_gp1_lock_acquire_release_cycle(tmp_path):
    from hermes_trader.client.lock import EntryOrderLock
    a = EntryOrderLock(lock_dir=str(tmp_path))
    b = EntryOrderLock(lock_dir=str(tmp_path))
    assert a.acquire() is True
    # Re-entrant for the same holder; a second process loses.
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_gp1_lock_release_is_idempotent(tmp_path):
    from hermes_trader.client.lock import EntryOrderLock
    a = EntryOrderLock(lock_dir=str(tmp_path))
    a.release()  # never acquired -> no-op, no raise
    assert a.acquire() is True
    a.release()
    a.release()  # double release after a real hold -> no-op
    assert EntryOrderLock(lock_dir=str(tmp_path)).acquire() is True


def test_gp1_lock_hold_context_raises_when_busy(tmp_path):
    from hermes_trader.client.lock import EntryOrderLock
    a = EntryOrderLock(lock_dir=str(tmp_path))
    b = EntryOrderLock(lock_dir=str(tmp_path))
    with a.hold():
        with pytest.raises(BlockingIOError):
            with b.hold():
                pass
    # Released on context exit.
    with b.hold():
        pass


def test_gp1_lock_acquire_no_unbound_local_when_open_fails(tmp_path, monkeypatch):
    """If os.open itself raises, acquire must return False (refuse) rather
    than crash with UnboundLocalError (would surface as HTTP 500)."""
    import hermes_trader.client.lock as lock_mod

    lock = lock_mod.EntryOrderLock(lock_dir=str(tmp_path))

    def _boom(*a, **k):
        raise OSError("lock dir vanished")

    monkeypatch.setattr(lock_mod.os, "open", _boom)
    assert lock.acquire() is False


# ──────────────────────────────────────────────────────────────────────────
# G-P1-2 + D-1: memory refresh whitelist
# ──────────────────────────────────────────────────────────────────────────

def test_gp1_disk_refresh_picks_up_daily_pnl_fields(tmp_path, monkeypatch):
    from hermes_trader.agents import memory as memory_mod

    mem_file = tmp_path / ".agent-memory.json"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_file))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(mem_file) + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    mem = memory_mod.AgentMemory()
    mem.load()
    assert mem.get_daily_pnl() == 0.0

    # The loop process writes a same-day drawdown; bump mtime into the future
    # so the mtime gate accepts it.
    mem_file.write_text(json.dumps({"dailyPnl": -312.5, "peakDailyPnl": 180.25}))
    future = time.time() + 10
    os.utime(mem_file, (future, future))

    mem.refresh_risk_state_from_disk()
    assert mem.get_daily_pnl() == pytest.approx(-312.5)
    assert mem.peak_daily_pnl() == pytest.approx(180.25)


def test_gp1_disk_refresh_tolerates_garbage_pnl_fields(tmp_path, monkeypatch):
    from hermes_trader.agents import memory as memory_mod

    mem_file = tmp_path / ".agent-memory.json"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_file))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(mem_file) + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    mem = memory_mod.AgentMemory()
    mem.load()
    mem_file.write_text(json.dumps({"dailyPnl": "not-a-number"}))
    future = time.time() + 10
    os.utime(mem_file, (future, future))
    mem.refresh_risk_state_from_disk()  # must not raise
    assert mem.get_daily_pnl() == 0.0


# ──────────────────────────────────────────────────────────────────────────
# G-P1-3: executor skips the tick when the cross-process lock is held
# ──────────────────────────────────────────────────────────────────────────

def _h6_like_baseline(monkeypatch):
    """Minimal executor I/O stub world, adapted from test_audit_h6_cm3."""
    from hermes_trader.agents import executor

    cfg = {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": True,
        "equity_fraction_per_trade": 0.10, "leverage": 10,
        "max_trade_notional_usd": 100000, "max_concurrent": 18,
        "max_total_notional_pct": 40.0, "max_daily_loss_usd": -1000,
        "min_available_margin_pct": 0.10, "cooldown_min": 60,
        "min_ai_confidence": 0.30, "counter_regime_min_conf": 0.65,
        "max_crypto_long_correlated": 5, "min_market_volume_usd": 5_000_000,
        "min_hip3_volume_usd": 500_000, "conviction_sizing": True,
        "debate_gate": {"enabled": False},
        "dsl_exit": {"max_loss_pct": 2.0, "max_loss_roe_pct": 30.0,
                     "protect_pct": 0.5, "retrace_threshold": 0.3,
                     "hard_timeout_minutes": 180.0},
        "circuit_breaker": {"resp_unknown_halt_n": 3,
                            "resp_unknown_halt_min": 60.0},
    }
    state = {"equity": 1000.0, "available": 500.0, "total_ntl": 0.0,
             "asset_positions": [], "dex_equity": {"": 1000.0},
             "dex_available": {"": 500.0}}
    calls = {"placed": 0}

    monkeypatch.setattr(executor, "read_agent_config", lambda: cfg)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xMASTER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: state)
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 2.0)
    monkeypatch.setattr(
        "hermes_trader.client.price_crosscheck.crosscheck_price",
        lambda coin, px: {"ok": True, "checked": False, "reason": "stub"})
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 40)
    monkeypatch.setattr(executor, "get_orderbook_spread",
                        lambda c: {"ok": True, "spread_pct": 0.01,
                                   "best_bid": 99.9, "best_ask": 100.1,
                                   "bid_depth_1pct_usd": 1e9,
                                   "ask_depth_1pct_usd": 1e9})
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "set_leverage", lambda c, l: {"ok": True})
    monkeypatch.setattr(executor, "place_hl_trigger_order",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda *a, **k: {"marginSummary": {"accountValue": "500"}})
    monkeypatch.setattr("hermes_trader.agents.market_regime.detect_regime_with_score",
                        lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr("hermes_trader.agents.hyperfeed.market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "regimes_by_class": {}})

    def _place(is_buy, size, mid, coin, **kw):
        calls["placed"] += 1
        return {"ok": True, "order_id": "OID1", "avg_px": mid}

    monkeypatch.setattr(executor, "place_hl_order", _place)
    monkeypatch.setattr(executor, "register_position", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "track_daily_pnl", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "peak_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "consecutive_losses", lambda coin: 0)
    monkeypatch.setattr(executor.memory,
                        "coin_daily_realized_pnl_pct", lambda coin, sod: 0.0)
    monkeypatch.setattr(executor.memory, "get_recent_trades", lambda n=10: [])
    monkeypatch.setattr(executor.memory, "record_trade", lambda t: None)
    monkeypatch.setattr(executor.memory, "set_global_halt", lambda until: None)
    monkeypatch.setattr(executor.memory, "global_halt_remaining_min", lambda: 0.0)
    monkeypatch.setattr("hermes_trader.agents.dsl_exit.rehydrate_from_exchange",
                        lambda *a, **k: None)
    monkeypatch.setattr("hermes_trader.client.exchange.verify_order_exists",
                        lambda **k: {"verified": True})
    monkeypatch.setattr("hermes_trader.notify.send_text", lambda *a, **k: None)
    executor._reset_resp_unknown_streak()
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xabc")
    return executor, calls


def _h6_analysis(**kw):
    base = {"id": str(uuid.uuid4()), "coin": "BTC", "verdict": "LONG",
            "side": "long", "confidence": 0.70, "composite_score": 30,
            "entry_px": 100, "stop_px": 95, "tp_px": 110,
            "news_context": "no news"}
    base.update(kw)
    return base


def test_gp1_executor_entry_lock_busy_skips_tick(tmp_path, monkeypatch):
    """With the flock held by 'another process', maybe_execute must skip with
    reason entry_lock_busy, place NO order, and roll back its in-flight
    markers so the next tick is not wedged."""
    from hermes_trader.client.lock import EntryOrderLock
    executor, calls = _h6_like_baseline(monkeypatch)

    iso_lock = EntryOrderLock(lock_dir=str(tmp_path))
    monkeypatch.setattr(executor, "_ENTRY_LOCK", iso_lock)
    holder = EntryOrderLock(lock_dir=str(tmp_path))
    assert holder.acquire() is True
    try:
        analysis = _h6_analysis()
        r = executor.maybe_execute(analysis)
        assert r["executed"] is False
        assert r["reason"] == "entry_lock_busy"
        assert calls["placed"] == 0
        # markers rolled back
        assert analysis["id"] not in executor._IN_FLIGHT_ANALYSES
        assert "BTC" not in executor._IN_FLIGHT_COINS
    finally:
        holder.release()
        iso_lock.release()


# ──────────────────────────────────────────────────────────────────────────
# Server route-level guards (TestClient against the real server.app)
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def manual_harness(tmp_path, monkeypatch):
    """Patch the full place_order I/O surface; returns (client, srv, env)."""
    from fastapi.testclient import TestClient

    from hermes_trader.client import exchange
    from hermes_trader.client.lock import EntryOrderLock
    from hermes_trader import server as srv

    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)

    env = {
        "reconcile": {"status": "not_filled", "reason": "stub"},
        "reconcile_calls": 0,
        "place_result": {"ok": True, "order_id": "OID1",
                         "avg_px": 100.0, "total_sz": 1.0},
        "alerts": [],
        "brackets_called": 0,
        "account_state": {"equity": 100000.0, "asset_positions": []},
    }

    iso_lock = EntryOrderLock(lock_dir=str(tmp_path))
    monkeypatch.setattr(srv, "_ENTRY_LOCK", iso_lock)

    monkeypatch.setattr(srv, "read_agent_config",
                        lambda: {"mode": "LIVE", "leverage": 10})

    async def _equity():
        return 100000.0

    monkeypatch.setattr(srv, "_fetch_live_equity", _equity)
    monkeypatch.setattr(srv, "resolve_user_address", lambda: "0xMASTER")

    def _fetch_acct(user, include_hip3=False):
        if callable(env["account_state"]):
            return env["account_state"]()
        return env["account_state"]

    monkeypatch.setattr(srv, "fetch_account_state", _fetch_acct)
    monkeypatch.setattr(srv, "_hip3_on", lambda: False)
    monkeypatch.setattr(srv, "_check_manual_order_gates",
                        lambda **k: {"blocked": False, "results": {},
                                     "block_reasons": []})
    monkeypatch.setattr(srv, "_sum_open_notional", lambda a: 0.0)

    # exchange surface (imported lazily INSIDE the handler -> patch module attrs)
    monkeypatch.setattr(exchange, "get_hl_price", lambda coin: 100.0)
    monkeypatch.setattr(exchange, "set_leverage",
                        lambda coin, lev: {"ok": True})
    monkeypatch.setattr(exchange, "get_hl_atr", lambda *a, **k: 2.0)
    monkeypatch.setattr(exchange, "min_entry_notional_usd",
                        lambda coin, mid: 10.0)
    monkeypatch.setattr(exchange, "entry_size_for_notional",
                        lambda coin, ntl, mid: ntl / mid)

    def _place(is_buy, size, mid, coin, **kw):
        return env["place_result"]

    monkeypatch.setattr(exchange, "place_hl_order", _place)

    def _reconcile(**kw):
        env["reconcile_calls"] += 1
        return env["reconcile"]

    monkeypatch.setattr(exchange, "reconcile_order_fill", _reconcile)

    # metaAndAssetCtxs volume read.
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda *a, **k: [{"coin": "BTC",
                                          "ctx": {"dayNtlVlm": "1000000000"}}])

    monkeypatch.setattr(srv.memory, "refresh_risk_state_from_disk", lambda: None)

    async def _noop_log(entry):
        return None

    monkeypatch.setattr(srv, "_append_session_log", _noop_log)

    def _brackets(**kw):
        env["brackets_called"] += 1
        return {"brackets": [], "warnings": [], "sl_missing": False}

    monkeypatch.setattr(srv, "_place_manual_post_fill_brackets", _brackets)

    from hermes_trader import notify
    monkeypatch.setattr(notify, "send_text",
                        lambda *a, **k: env["alerts"].append(a[0] if a else ""))

    client = TestClient(srv.app)
    env["client"] = client
    env["lock"] = iso_lock
    return client, srv, env


def _post(client, **over):
    body = {"coin": "BTC", "side": "long", "leverage": 5,
            "riskUSD": 1000.0}
    body.update(over)
    return client.post("/api/hl/place-order", json=body, headers=_auth())


def test_gp1_route_lock_busy_returns_409(tmp_path, manual_harness):
    """Another process holding the entry flock -> manual order gets 409
    entry_lock_busy (and the flock is NOT taken by the handler)."""
    from hermes_trader.client.lock import EntryOrderLock
    client, srv, env = manual_harness
    holder = EntryOrderLock(lock_dir=str(tmp_path))
    assert holder.acquire() is True
    try:
        r = _post(client)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "entry_lock_busy"
        # No exchange order, no reconcile.
        assert env["reconcile_calls"] == 0
        assert env["brackets_called"] == 0
    finally:
        holder.release()
        env["lock"].release()


def test_gp1_route_pre_place_reread_failure_is_503_fail_closed(manual_harness):
    """If the live account read fails at the pre-place re-check (the SECOND
    account read; the gate-stage read must still succeed), the manual order
    is refused with 503 pre_place_recheck_failed (never guesses 'no position'
    and double-opens)."""
    client, srv, env = manual_harness

    calls = {"n": 0}

    def _phase_fake(user=None, include_hip3=False):
        calls["n"] += 1
        # 1st read: gate-stage snapshot (succeeds, no positions).
        if calls["n"] == 1:
            return {"equity": 100000.0, "asset_positions": []}
        # 2nd read: the pre-place re-check fails.
        raise ConnectionError("userFills/deaf clearinghouse")

    env["account_state"] = _phase_fake
    try:
        r = _post(client)
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "pre_place_recheck_failed"
        assert env["reconcile_calls"] == 0
        assert env["brackets_called"] == 0
    finally:
        env["lock"].release()


def test_hp1_route_gate_stage_account_read_failure_is_503_fail_closed(manual_harness):
    """H-P1: the FIRST account read (before the 22-gate chain) used to
    degrade to acct={} on failure, so exposure/notional gates evaluated
    against a zero-positions fiction. It must now refuse with 503
    account_state_unavailable before any order is placed."""
    client, srv, env = manual_harness

    def _boom(*a, **k):
        raise ConnectionError("clearinghouse down")

    env["account_state"] = _boom
    try:
        r = _post(client)
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "account_state_unavailable"
        assert env["reconcile_calls"] == 0
        assert env["brackets_called"] == 0
    finally:
        env["lock"].release()


def test_hp1_route_non_positive_atr_is_503(manual_harness, monkeypatch):
    """H-P1: without a positive 4h ATR the post-fill bracket helper would arm
    NO stop and leave the new position naked. The order must be refused with
    503 atr_unavailable before placement."""
    from hermes_trader.client import exchange
    client, srv, env = manual_harness
    monkeypatch.setattr(exchange, "get_hl_atr", lambda *a, **k: 0.0)
    try:
        r = _post(client)
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "atr_unavailable"
        assert env["reconcile_calls"] == 0
        assert env["brackets_called"] == 0
    finally:
        env["lock"].release()


def test_gp1_route_response_unknown_not_filled_is_400(manual_harness):
    """Envelope lost + userFills confirms no fill -> safe 400, no bracket,
    no LOUD unresolved alert."""
    client, srv, env = manual_harness
    env["place_result"] = {"ok": False, "error": "408 timeout",
                           "error_code": "response_unknown"}
    env["reconcile"] = {"status": "not_filled", "reason": "no_matching_fill"}
    try:
        r = _post(client)
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "order_response_unknown_not_filled"
        assert env["reconcile_calls"] == 1
        assert env["brackets_called"] == 0
        assert env["alerts"] == []
    finally:
        env["lock"].release()


def test_gp1_route_response_unknown_unresolved_is_503_and_alerted(manual_harness):
    """Envelope lost + fill state unknowable -> fail-closed 503 and a LOUD
    alert telling the operator not to resubmit blindly."""
    client, srv, env = manual_harness
    env["place_result"] = {"ok": False, "error": "conn reset",
                           "error_code": "response_unknown"}
    env["reconcile"] = {"status": "unknown",
                        "reason": "userFills_fetch_exception: boom"}
    try:
        r = _post(client)
        assert r.status_code == 503
        assert r.json()["detail"]["error"] == "order_response_unknown_unresolved"
        assert env["brackets_called"] == 0
        assert env["alerts"] and "切勿盲目重发" in env["alerts"][0]
    finally:
        env["lock"].release()


def test_gp1_route_response_unknown_filled_backfills_and_brackets(manual_harness):
    """Envelope lost but userFills confirms a FILL -> the result is backfilled
    with the real economics, the bracket path still runs (no orphan), and the
    200 response carries the reconciled marker + an info alert."""
    client, srv, env = manual_harness
    env["place_result"] = {"ok": False, "error": "502",
                           "error_code": "response_unknown"}
    env["reconcile"] = {"status": "filled", "avg_px": 100.5,
                        "total_sz": 9.95, "filled_at_ms": 1234567890,
                        "oid": "OID999", "cloid": "0xx", "n_fills": 1}
    try:
        r = _post(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reconciled_after_response_unknown"] is True
        assert body["order_id"] == "OID999"
        assert body["entryPrice"] == pytest.approx(100.5)
        assert body["size"] == pytest.approx(9.95)
        assert env["brackets_called"] == 1
        assert env["alerts"] and "已补登并挂保护单" in env["alerts"][0]
    finally:
        env["lock"].release()


def test_gp1_route_normal_success_still_works(manual_harness):
    """Baseline sanity: an ordinary successful envelope is untouched by the
    new reconcile/lock machinery and returns 200."""
    client, srv, env = manual_harness
    try:
        r = _post(client)
        assert r.status_code == 200, r.text
        assert env["reconcile_calls"] == 0
        assert env["brackets_called"] == 1
    finally:
        env["lock"].release()


# ──────────────────────────────────────────────────────────────────────────
# Source guards (the cross-process wiring is structural, not just behavior)
# ──────────────────────────────────────────────────────────────────────────

def test_gp1_source_server_imports_shared_lock_singleton():
    with open(SERVER_PY) as f:
        src = f.read()
    # The server must share the EXECUTOR's singleton, not construct its own.
    assert "_ENTRY_LOCK," in src
    assert "EntryOrderLock()" not in src
    assert '"entry_lock_busy"' in src


def test_gp1_source_server_flock_ordering_and_release():
    """Acquire happens after the in-process claim but BEFORE exchange I/O;
    release lives in the handler finally."""
    from hermes_trader import server as srv
    src = inspect.getsource(srv.place_order)
    i_claim = src.index("_IN_FLIGHT_COINS.add(coin)")
    i_acq = src.index("_ENTRY_LOCK.acquire()")
    i_price = src.index("get_hl_price")
    i_recheck = src.index('"pre_place_recheck_failed"')
    i_release = src.index("_ENTRY_LOCK.release()")
    assert i_claim < i_acq < i_price < i_recheck < i_release
    # Fail-closed semantics present; no fail-open comment on this re-check.
    assert "fail-CLOSED" in src
    # The three-state reconcile block is present in the entry path.
    assert '"order_response_unknown_not_filled"' in src
    assert '"order_response_unknown_unresolved"' in src
    assert "reconciled_after_response_unknown" in src


def test_gp1_source_run_execute_refreshes_before_maybe_execute():
    from hermes_trader import server as srv
    src = inspect.getsource(srv.run_execute)
    # Compare actual call sites, ignoring words that appear in comments first.
    refresh_call = src.index("memory.refresh_risk_state_from_disk()")
    execute_call = src.index("maybe_execute(analysis)")
    assert refresh_call < execute_call


def test_gp1_source_executor_lock_wiring():
    with open(EXECUTOR_PY) as f:
        src = f.read()
    assert "_ENTRY_LOCK = _EntryOrderLock()" in src
    assert '"entry_lock_busy"' in src
    # One acquire, releases on every early-exit + register finally.
    assert src.count("_ENTRY_LOCK.acquire()") == 1
    assert src.count("_ENTRY_LOCK.release()") >= 5


def test_gp1_source_memory_whitelist_has_daily_pnl():
    from hermes_trader.agents import memory as memory_mod
    src = inspect.getsource(memory_mod.AgentMemory.refresh_risk_state_from_disk)
    assert 'data.get("dailyPnl"' in src
    assert 'data.get("peakDailyPnl"' in src
