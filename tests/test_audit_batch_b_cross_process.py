"""Audit batch B (2026-09-09): cross-process risk-state family.

Covers the six minimal, in-place hardening edits:

* P1-1  server.py manual-order endpoint refreshes risk state from disk
        before evaluating the hard kill-switch gates (source-level guard).
* P1-2  MCP cancel_order refuses to cancel a DSL-managed SL/TP trigger;
        a tracker-lookup failure fails closed (no cancel).
* P1-3a executor maybe_execute checks the set_leverage return value,
        retries once, and fails closed before the in-flight claim/order.
* P1-3b MCP set_leverage refuses while a position is open (incl. HIP-3
        ``dex:COIN`` names), fails closed on account-read error, audits.
* P2-4  MCP config write goes through the single-LOCK_EX RMW
        update_agent_config; a bare read never rewrites; a missing raw
        file surfaces RuntimeError instead of blind overwrite.
* P2-5  external-close backfill arms the same tiered-breaker chain as
        the close chokepoint via arm_close_tiered_breakers
        (loss streak / per-coin circuit / global daily halt).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_trader.agents import executor
from hermes_trader.agents import memory as memory_mod
from hermes_trader.agents import config_store

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SERVER_PY = ROOT / "hermes_trader" / "server.py"


# ── helpers ───────────────────────────────────────────────────────────────

def _load_mcp():
    """Load scripts/hermes-mcp-server.py as an isolated module."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_mcp_server_batch_b", SCRIPTS / "hermes-mcp-server.py")
        mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp)
        mcp._HERMES_MCP_ALLOW_WRITE = True
        return mcp
    finally:
        try:
            sys.path.remove(str(SCRIPTS))
        except ValueError:
            pass


def _isolated_memory(monkeypatch, tmp_path):
    """AgentMemory pointed at tmp paths, hydrated fresh."""
    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    m = memory_mod.AgentMemory()
    m.load()
    return m


def _isolate_dsl(monkeypatch, tmp_path):
    """Point DSL state at tmp paths so the handler's load_state(force=True)
    never touches the real on-disk registry. The force reload clears
    in-memory trackers when no file exists (dsl_exit.py L1811), so positions
    registered AFTER this call are persisted to the tmp file by
    register_position/set_bracket and survive the handler's reload."""
    from hermes_trader.agents import dsl_exit
    state_path = str(tmp_path / "dsl-state.json")
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", state_path)
    monkeypatch.setattr(dsl_exit, "DSL_STATE_LOCK_FILE", state_path + ".lock")
    dsl_exit.reset_force_load_throttle()
    dsl_exit._active_positions.clear()
    return dsl_exit


def _stub_event_log(monkeypatch, append):
    # Handlers do a function-local `from hermes_trader import event_log`, so
    # the package attribute (not the MCP module attribute) must be patched.
    # Import first: the attribute only exists once the submodule is loaded.
    import hermes_trader.event_log  # noqa: F401
    monkeypatch.setattr(
        "hermes_trader.event_log",
        type("_L", (), {"append": staticmethod(append)})())


def _silent_event_log(monkeypatch):
    _stub_event_log(monkeypatch, lambda *a, **k: None)


# ── P1-1: server refreshes risk state before hard-gate eval ──────────────

def test_p1_1_server_refreshes_risk_state_before_gates():
    """Source-level guard: the manual-order endpoint must call
    memory.refresh_risk_state_from_disk() before _check_manual_order_gates
    so the API process never evaluates hard kill-switches on a startup
    snapshot."""
    src = SERVER_PY.read_text(encoding="utf-8")
    anchor = src.index("refresh_risk_state_from_disk()")
    gates = src.index("_check_manual_order_gates", anchor)
    assert anchor < gates, "refresh must precede gate evaluation"
    # one manual-entry call site, one refresh (no double refresh)
    assert src.count("memory.refresh_risk_state_from_disk()") == 1


# ── P1-2: MCP cancel_order refuses DSL bracket oids ──────────────────────

def test_p1_2_cancel_refuses_dsl_sl_oid(monkeypatch, tmp_path):
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    mcp = _load_mcp()
    tr = dsl_exit.register_position("ETH", "long", 2500.0)
    tr.sl_oid = 111
    tr.tp_oid = 222
    dsl_exit._save_state()  # persist oids for the handler's force reload

    cancelled = {"n": 0}

    def _cancel(*a, **k):
        cancelled["n"] += 1
        return {"ok": True}

    monkeypatch.setattr("hermes_trader.client.exchange.cancel_orders", _cancel)
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_cancel_order({"asset": 4, "order_id": 111}))
    dsl_exit._active_positions.clear()
    assert out["cancelled"] is False
    assert "DSL-managed SL" in out["error"]
    assert cancelled["n"] == 0


def test_p1_2_cancel_refuses_dsl_tp_oid(monkeypatch, tmp_path):
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    mcp = _load_mcp()
    dsl_exit.register_position("BTC", "short", 100000.0)
    dsl_exit.set_bracket("BTC", "short", sl_oid=333, tp_oid=444)
    cancelled = {"n": 0}
    monkeypatch.setattr("hermes_trader.client.exchange.cancel_orders",
                        lambda *a, **k: cancelled.update(n=cancelled["n"] + 1)
                        or {"ok": True})
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_cancel_order({"asset": 1, "order_id": 444}))
    dsl_exit._active_positions.clear()
    assert out["cancelled"] is False
    assert "DSL-managed TP" in out["error"]
    assert cancelled["n"] == 0


def test_p1_2_cancel_lookup_failure_fails_closed(monkeypatch, tmp_path):
    """If the tracker read itself fails, the cancel must NOT proceed."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    mcp = _load_mcp()
    cancelled = {"n": 0}
    monkeypatch.setattr(dsl_exit, "load_state",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk io")))
    monkeypatch.setattr("hermes_trader.client.exchange.cancel_orders",
                        lambda *a, **k: cancelled.update(n=cancelled["n"] + 1)
                        or {"ok": True})
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_cancel_order({"asset": 1, "order_id": 999}))
    assert out["cancelled"] is False
    assert "could not verify DSL bracket ownership" in out["error"]
    assert cancelled["n"] == 0


def test_p1_2_cancel_unrelated_oid_proceeds(monkeypatch, tmp_path):
    """An oid that is not a DSL bracket oid must still cancel normally."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    mcp = _load_mcp()
    dsl_exit.register_position("ETH", "long", 2500.0)
    dsl_exit.set_bracket("ETH", "long", sl_oid=111, tp_oid=222)
    monkeypatch.setattr("hermes_trader.client.exchange.cancel_orders",
                        lambda oid, asset_idx=None: {"ok": True, "oid": oid})
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_cancel_order({"asset": 4, "order_id": 777}))
    dsl_exit._active_positions.clear()
    assert out["cancelled"] is True


# ── P1-3a: executor set_leverage return-value check / fail-closed ────────

def _live_harness(monkeypatch, *, lev_calls):
    """Wire maybe_execute LIVE far enough to reach the set_leverage call."""
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
    monkeypatch.setattr(executor, "entry_size_for_notional", lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "eval_all_gates",
                        lambda ctx, cfg, lt, **kw: {"blocked": False, "results": {}})
    monkeypatch.setattr(executor, "get_orderbook_spread",
                        lambda c: {"ok": True, "spread_pct": 0.01,
                                   "bid_depth_1pct_usd": 1e9,
                                   "ask_depth_1pct_usd": 1e9,
                                   "best_bid": 99.99, "best_ask": 100.01})
    # Patch only sleep; the rest of the real time module (perf_counter,
    # monotonic, …) must keep working on the longer retry-success path.
    monkeypatch.setattr(executor.time, "sleep",
                        lambda s: lev_calls.setdefault("sleeps", []).append(s))

    def _set_lev(c, lev):
        lev_calls["n"] = lev_calls.get("n", 0) + 1
        res = lev_calls["results"][min(lev_calls["n"] - 1,
                                       len(lev_calls["results"]) - 1)]
        return res

    monkeypatch.setattr(executor, "set_leverage", _set_lev)
    # Default: no order may land. The retry-success test overrides this.
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **k: pytest.fail("order must not be placed"))


def test_p1_3a_set_leverage_double_failure_aborts(monkeypatch):
    lev_calls = {"results": [{"ok": False, "error": "boom"},
                             {"ok": False, "error": "boom"}]}
    _live_harness(monkeypatch, lev_calls=lev_calls)
    res = executor.maybe_execute({
        "id": "lev-fail", "coin": "TEST", "verdict": "LONG",
        "side": "long", "confidence": 0.9, "composite_score": 80.0,
    })
    assert res["executed"] is False
    assert res["reason"].startswith("set_leverage_failed:")
    assert lev_calls["n"] == 2            # exactly one retry
    assert lev_calls.get("sleeps") == [0.5]


def test_p1_3a_set_leverage_retry_success_proceeds(monkeypatch):
    """A transient failure followed by success must NOT abort at the
    leverage gate (execution proceeds past it, all the way to order
    placement)."""
    lev_calls = {"results": [{"ok": False, "error": "transient"},
                             {"ok": True}]}
    _live_harness(monkeypatch, lev_calls=lev_calls)
    # The liq-buffer gate is best-effort over the same stubbed account read
    # ("no_existing_position" -> ok anyway); pin it to keep the path honest.
    monkeypatch.setattr(executor, "_check_liquidation_buffer",
                        lambda *a, **k: {"ok": True})
    # Reaching place_hl_order proves the leverage gate was passed. Raise a
    # sentinel there instead of returning a fill, so no DSL/memory state
    # gets registered by a fake successful order.
    def _place(*a, **k):
        raise RuntimeError("SENTINEL past leverage gate")

    monkeypatch.setattr(executor, "place_hl_order", _place)
    analysis = {
        "id": "lev-retry-ok", "coin": "TEST", "verdict": "LONG",
        "side": "long", "confidence": 0.9, "composite_score": 80.0,
    }
    raised = False
    res = None
    try:
        res = executor.maybe_execute(analysis)
    except RuntimeError as e:
        assert "SENTINEL past leverage gate" in str(e)
        raised = True
    finally:
        # maybe_execute has no finally clause around place_hl_order, so the
        # in-flight claim must be cleaned manually to avoid cross-test leak.
        with executor._EXEC_LOCK:
            executor._IN_FLIGHT_ANALYSES.discard("lev-retry-ok")
            executor._IN_FLIGHT_COINS.discard("TEST")
    assert lev_calls["n"] == 2
    assert raised or not str(
        (res or {}).get("reason", "")).startswith("set_leverage_failed:")


# ── P1-3b: MCP set_leverage position guard ───────────────────────────────

def test_p1_3b_set_leverage_blocked_with_open_native_position(monkeypatch):
    mcp = _load_mcp()
    monkeypatch.setattr(mcp, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(mcp, "fetch_account_state", lambda u, include_hip3=False: {
        "asset_positions": [{"position": {"coin": "ETH", "szi": "0.5"}}]})
    called = {"n": 0}
    monkeypatch.setattr("hermes_trader.client.exchange.set_leverage",
                        lambda *a, **k: called.update(n=called["n"] + 1)
                        or {"ok": True})
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_set_leverage({"coin": "ETH", "leverage": 3}))
    assert out["ok"] is False
    assert "open position" in out["error"]
    assert called["n"] == 0


def test_p1_3b_set_leverage_blocked_with_hip3_namespaced_position(monkeypatch):
    """HIP-3 positions come back as ``dex:COIN`` (e.g. ``xyz:MU``); the
    guard must normalise both sides and still match."""
    mcp = _load_mcp()
    monkeypatch.setattr(mcp, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(mcp, "fetch_account_state", lambda u, include_hip3=False: {
        "asset_positions": [{"position": {"coin": "xyz:MU", "szi": "-100"}}]})
    called = {"n": 0}
    monkeypatch.setattr("hermes_trader.client.exchange.set_leverage",
                        lambda *a, **k: called.update(n=called["n"] + 1)
                        or {"ok": True})
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_set_leverage({"coin": "xyz:mu", "leverage": 5}))
    assert out["ok"] is False
    assert "open position" in out["error"]
    assert called["n"] == 0


def test_p1_3b_set_leverage_allowed_when_flat(monkeypatch):
    mcp = _load_mcp()
    monkeypatch.setattr(mcp, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(mcp, "fetch_account_state", lambda u, include_hip3=False: {
        "asset_positions": [
            {"position": {"coin": "ETH", "szi": "0"}},
            {"position": {"coin": "BTC", "szi": 0}},
        ]})
    monkeypatch.setattr("hermes_trader.client.exchange.set_leverage",
                        lambda c, lev: {"ok": True, "coin": c, "leverage": lev})
    audit = []
    _stub_event_log(monkeypatch,
                    lambda kind, payload=None: audit.append((kind, payload)))

    out = json.loads(mcp.handle_set_leverage({"coin": "SOL", "leverage": 7}))
    assert out["ok"] is True
    assert audit and audit[-1][1]["action"] == "set_leverage"
    assert audit[-1][1]["ok"] is True


def test_p1_3b_set_leverage_account_read_failure_fails_closed(monkeypatch):
    mcp = _load_mcp()
    monkeypatch.setattr(mcp, "resolve_user_address", lambda: "0xUSER")

    def _boom(*a, **k):
        raise RuntimeError("account api 500")

    monkeypatch.setattr(mcp, "fetch_account_state", _boom)
    called = {"n": 0}
    monkeypatch.setattr("hermes_trader.client.exchange.set_leverage",
                        lambda *a, **k: called.update(n=called["n"] + 1)
                        or {"ok": True})
    _silent_event_log(monkeypatch)

    out = json.loads(mcp.handle_set_leverage({"coin": "ETH", "leverage": 3}))
    assert out["ok"] is False
    assert "could not verify" in out["error"]
    assert called["n"] == 0


# ── P2-4: MCP config single-lock RMW ─────────────────────────────────────

def _tmp_config(monkeypatch, tmp_path, raw):
    cfg_file = tmp_path / ".agent-config.json"
    if raw is not None:
        cfg_file.write_text(json.dumps(raw))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")
    config_store._RAW_CACHE = None
    config_store._RAW_CACHE_SIG = None
    return cfg_file


def test_p2_4_bare_read_does_not_write(monkeypatch, tmp_path):
    _tmp_config(monkeypatch, tmp_path, {"leverage": 5})
    mcp = _load_mcp()
    out = json.loads(mcp.handle_config({}))
    assert out.get("leverage") == 5
    # No backup file is created by a bare read.
    assert not (tmp_path / ".agent-config.json.bak").exists()


def test_p2_4_write_goes_through_rmw_and_persists(monkeypatch, tmp_path):
    _tmp_config(monkeypatch, tmp_path, {"leverage": 5})
    mcp = _load_mcp()
    out = json.loads(mcp.handle_config({"leverage": 9}))
    assert out["leverage"] == 9
    # Persisted through the single-lock RMW path (raw file reflects change).
    on_disk = json.loads((tmp_path / ".agent-config.json").read_text())
    assert on_disk["leverage"] == 9


def test_p2_4_nested_toggle_flattened(monkeypatch, tmp_path):
    _tmp_config(monkeypatch, tmp_path, {"momentum_continuation": {"enabled": False}})
    mcp = _load_mcp()
    out = json.loads(mcp.handle_config({"momentum_continuation_enabled": True}))
    assert out["momentum_continuation"]["enabled"] is True
    on_disk = json.loads((tmp_path / ".agent-config.json").read_text())
    assert on_disk["momentum_continuation"]["enabled"] is True


def test_p2_4_missing_raw_config_refuses_blind_write(monkeypatch, tmp_path):
    _tmp_config(monkeypatch, tmp_path, None)
    mcp = _load_mcp()
    out = json.loads(mcp.handle_config({"leverage": 9}))
    assert "error" in out
    # No blind file creation.
    assert not (tmp_path / ".agent-config.json").exists()


# ── P2-5: arm_close_tiered_breakers ──────────────────────────────────────

class _BreakerSpy:
    """Records risk-blocking calls; serves the read APIs the chain needs."""

    def __init__(self, *, daily_pnl=0.0, sod=1000.0, global_remaining=0.0):
        self.losses = {}
        self.coin_circuits = {}
        self.global_halt_until = None
        self._daily_pnl = daily_pnl
        self._sod = sod
        self._global_remaining = global_remaining

    def record_loss_outcome(self, coin, pct):
        self.losses[coin] = self.losses.get(coin, 0) + (1 if pct < 0 else 0)
        if pct >= 0:
            self.losses[coin] = 0

    def set_coin_circuit(self, coin, until_ms):
        self.coin_circuits[coin] = until_ms

    def set_global_halt(self, until_ms):
        self.global_halt_until = until_ms

    def get_daily_pnl(self):
        return self._daily_pnl

    def get_start_of_day_equity(self):
        return self._sod

    def global_halt_remaining_min(self):
        return self._global_remaining


def test_p2_5_loss_streak_and_coin_circuit_armed(monkeypatch):
    spy = _BreakerSpy()
    monkeypatch.setattr(executor, "memory", spy)
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "circuit_breaker": {
            "single_coin_loss_pct": 3.0, "single_coin_halt_min": 60.0,
            "daily_loss_pct": 5.0, "daily_halt_min": 120.0,
        }})
    # -4% unlevered spot move, -20% ROE.
    executor.arm_close_tiered_breakers("ETH", -4.0, -20.0, source="exchange_trigger")
    assert spy.losses["ETH"] == 1
    assert "ETH" in spy.coin_circuits
    assert spy.global_halt_until is None


def test_p2_5_global_daily_breaker_armed(monkeypatch):
    spy = _BreakerSpy(daily_pnl=-60.0, sod=1000.0)  # -6% daily
    monkeypatch.setattr(executor, "memory", spy)
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "circuit_breaker": {
            "single_coin_loss_pct": 3.0, "single_coin_halt_min": 60.0,
            "daily_loss_pct": 5.0, "daily_halt_min": 120.0,
        }})
    executor.arm_close_tiered_breakers("BTC", -1.0, -5.0, source="exchange_trigger")
    assert spy.global_halt_until is not None


def test_p2_5_small_move_arms_nothing_but_resets_streak(monkeypatch):
    spy = _BreakerSpy()
    spy.losses["SOL"] = 2
    monkeypatch.setattr(executor, "memory", spy)
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "circuit_breaker": {
            "single_coin_loss_pct": 3.0, "single_coin_halt_min": 60.0,
            "daily_loss_pct": 5.0, "daily_halt_min": 120.0,
        }})
    # Winning close: +1% spot, +5% ROE.
    executor.arm_close_tiered_breakers("SOL", 1.0, 5.0)
    assert spy.losses["SOL"] == 0
    assert "SOL" not in spy.coin_circuits
    assert spy.global_halt_until is None


def test_p2_5_never_raises_on_memory_error(monkeypatch):
    class _Boom(_BreakerSpy):
        def record_loss_outcome(self, coin, pct):
            raise RuntimeError("disk full")

    monkeypatch.setattr(executor, "memory", _Boom())
    monkeypatch.setattr(executor, "read_agent_config", lambda: {})
    # Must return None, not raise (backfill path cannot crash the loop).
    assert executor.arm_close_tiered_breakers("ETH", -4.0, -20.0) is None


def test_p2_5_backfill_call_site_wired_in_loop():
    """Source-level guard: the external-close backfill in trading_loop
    must call arm_close_tiered_breakers with source='exchange_trigger'."""
    src = (ROOT / "scripts" / "trading_loop.py").read_text(encoding="utf-8")
    assert "arm_close_tiered_breakers," in src  # imported
    anchor = src.index("arm_close_tiered_breakers(")
    # The call arguments span multiple indented lines; widen the window.
    snippet = src[anchor:anchor + 500]
    assert "source=\"exchange_trigger\"" in snippet
