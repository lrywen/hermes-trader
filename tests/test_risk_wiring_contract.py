"""Risk-wiring contract tests (2026-09-12).

ZERO production-behaviour change: this module only pins down how the
existing risk-control pieces are wired together, so the upcoming P1
alignment of the hard kill-switch with effective_daily_loss_cutoff()
(and any later touch of these paths) must change a test on purpose
rather than silently break a cross-module contract.

Contracts locked here (file:line anchors in each test):

  A. HARD daily-loss kill-switch inline block (scripts/trading_loop.py
     :1258-1274) — executes the REAL production statements, AST-
     extracted because importing the module runs its module-level
     ``while True`` and the block is (deliberately, this batch) not
     refactored into a helper:
       * breach + equity + positions -> every coin flattened exactly once
       * equity <= 0 / empty book -> never flattens (degraded-read guard)
       * above the floor -> never flattens
       * one coin's close raising never blocks the other coins
       * emits exactly one hard_killswitch event with the USD floor
       * KNOWN P1 ASYMMETRY: it reads max_daily_loss_usd ONLY — the
         equity-% leg (daily_loss_pct) can not trip it, while the
         entry gate's effective_daily_loss_cutoff() takes the tighter
         of pct/USD. A unit test demonstrates the divergence window.

  B. The five flatten paths never consult the LIVE entry grant
     (HERMES_ENABLE_LIVE); close_position_market always reaches the
     exchange as reduce_only, including through the bm11 breaker
     helper bound to the real close.

  C. Tick ordering (source contract): kill-switch -> bm11 -> DSL exits
     -> market_circuit_tick all sit BEFORE the mode==OFF ``continue``
     (OFF still monitors/flattens); scan/route_verdict sit AFTER it.

  D. Gate-vs-kill asymmetry: daily_loss_kill_switch gates on the
     tighter cutoff; the inline hard flatten gates on USD only — pinned
     so the P1 fix flips these tests deliberately.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TRADING_LOOP_SRC = (REPO_ROOT / "scripts" / "trading_loop.py").read_text(encoding="utf-8")


# ── AST harness ────────────────────────────────────────────────────────────
# importing scripts/trading_loop.py runs the module-level while-True loop, so
# loop logic is tested by AST-extracting nodes into controlled namespaces
# (same pattern as test_audit_p1._load_loop_fn / test_market_circuit._load_tick).

def _extract_function(name):
    tree = ast.parse(TRADING_LOOP_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == name)
    return ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))


def _load_bm11(*, flattener, event_log):
    """Compile bm11_breaker_flatten with an injected default flattener, so
    the helper's DEFAULT BINDING (production: close_position_market) is the
    only substituted name — its internal wiring runs verbatim."""
    from hermes_trader.agents.config_store import cfg_get

    ns = {
        "cfg_get": cfg_get,
        "logger": logging.getLogger("test.contract.bm11"),
        "close_position_market": flattener,
        "log_event": event_log,
    }
    exec(compile(_extract_function("bm11_breaker_flatten"),
                 "<trading_loop extracted>", "exec"), ns)
    return ns["bm11_breaker_flatten"]


def _iter_with_parent(node, parent=None):
    for child in ast.iter_child_nodes(node):
        yield child, node
        yield from _iter_with_parent(child, node)


def _load_killswitch_block():
    """Extract the inline HARD kill-switch block (Assign _max_daily_loss +
    the following If) from the main while-body's Try node. The block has no
    function name in production; locating it structurally (by the
    ``_max_daily_loss`` assignment target) instead of by line number keeps
    the harness working across harmless line drift."""
    tree = ast.parse(TRADING_LOOP_SRC)
    loop = next(n for n in tree.body if isinstance(n, ast.While))
    assign, holder = next(
        (c, p) for c, p in _iter_with_parent(loop)
        if isinstance(c, ast.Assign)
        and any(getattr(t, "id", "") == "_max_daily_loss" for t in c.targets))
    idx = holder.body.index(assign)
    guard = holder.body[idx + 1]
    assert isinstance(guard, ast.If), "kill-switch block shape drifted"
    block = ast.fix_missing_locations(
        ast.Module(body=[assign, guard], type_ignores=[]))

    def _run(cfg, equity, positions, daily_pnl, flattener, events):
        from hermes_trader.agents.config_store import cfg_get as _cfg_get

        ns = {
            "cfg_get": _cfg_get,
            "_cfg": cfg,
            "equity": float(equity),
            "positions": positions,
            "daily_pnl": float(daily_pnl),
            "close_position_market": flattener,
            "log_event": lambda e: events.append(e),
            "logger": logging.getLogger("test.contract.killswitch"),
        }
        exec(compile(block, "<trading_loop killswitch extracted>", "exec"), ns)

    return _run


def _positions(*coins):
    return [{"position": {"coin": c}} for c in coins]


# ── A. HARD daily-loss kill-switch inline block ────────────────────────────

def test_ks_breach_flattens_every_position_once_and_logs():
    run = _load_killswitch_block()
    flattened, events = [], []
    run(cfg={"max_daily_loss_usd": -3.0},
        equity=100.0, positions=_positions("BTC", "ETH", "SOL"),
        daily_pnl=-3.5,
        flattener=lambda c: flattened.append(c) or {"ok": True},
        events=events)
    assert flattened == ["BTC", "ETH", "SOL"]
    ev = [e for e in events if e.get("event") == "hard_killswitch"]
    assert len(ev) == 1
    assert ev[0] == {"event": "hard_killswitch", "daily_pnl": -3.5,
                     "limit": -3.0, "flattened": 3}


@pytest.mark.parametrize("equity,positions,pnl", [
    (0.0, _positions("BTC"), -10.0),       # degraded account read
    (-1.0, _positions("BTC"), -10.0),      # non-positive equity
    (100.0, [], -10.0),                    # flat book
    (100.0, _positions("BTC"), -2.99),     # above the floor
    (100.0, _positions("BTC"), -3.0 + 1e-9),
])
def test_ks_guard_no_flatten(equity, positions, pnl):
    run = _load_killswitch_block()
    flattened, events = [], []
    run({"max_daily_loss_usd": -3.0}, equity, positions, pnl,
        flattener=lambda c: flattened.append(c) or {"ok": True},
        events=events)
    assert flattened == []
    assert not any(e.get("event") == "hard_killswitch" for e in events)


def test_ks_boundary_equality_fires():
    # `daily_pnl <= _max_daily_loss` — the boundary itself flattens.
    run = _load_killswitch_block()
    flattened = []
    run({"max_daily_loss_usd": -3.0}, 100.0, _positions("BTC"), -3.0,
        flattener=lambda c: flattened.append(c) or {"ok": True}, events=[])
    assert flattened == ["BTC"]


def test_ks_one_coin_failure_never_blocks_others():
    run = _load_killswitch_block()
    flattened = []

    def _flattener(coin):
        if coin == "ETH":
            raise RuntimeError("exchange down")
        flattened.append(coin)
        return {"ok": True}

    events = []
    run({"max_daily_loss_usd": -3.0}, 100.0,
        _positions("BTC", "ETH", "SOL"), -5.0, _flattener, events)
    assert flattened == ["BTC", "SOL"]  # ETH raised; siblings still closed
    # the block still reports the full position count and never re-raises
    ev = next(e for e in events if e["event"] == "hard_killswitch")
    assert ev["flattened"] == 3


def test_ks_coins_without_coin_key_are_skipped():
    run = _load_killswitch_block()
    flattened = []
    bad_pos = [{"position": {"coin": "BTC"}},
               {"position": {}},
               {"metadata": True}]
    run({"max_daily_loss_usd": -3.0}, 100.0, bad_pos, -5.0,
        flattener=lambda c: flattened.append(c) or {"ok": True}, events=[])
    assert flattened == ["BTC"]


# ── B/D. USD-only semantics — the KNOWN P1 asymmetry ───────────────────────

def test_ks_block_does_not_reference_pct_leg():
    """Source contract: the hard kill-switch reads ONLY max_daily_loss_usd.
    daily_loss_pct / effective_daily_loss_cutoff must not appear inside the
    block. The P1 alignment will replace this block; that change must update
    this test in the same commit."""
    tree = ast.parse(TRADING_LOOP_SRC)
    loop = next(n for n in tree.body if isinstance(n, ast.While))
    assign, holder = next(
        (c, p) for c, p in _iter_with_parent(loop)
        if isinstance(c, ast.Assign)
        and any(getattr(t, "id", "") == "_max_daily_loss" for t in c.targets))
    idx = holder.body.index(assign)
    seg = ast.get_source_segment(TRADING_LOOP_SRC, holder.body[idx + 1]) or ""
    block_src = ast.get_source_segment(TRADING_LOOP_SRC, assign) + seg
    assert "max_daily_loss_usd" in block_src
    assert "daily_loss_pct" not in block_src
    assert "effective_daily_loss_cutoff" not in block_src


def test_pct_leg_can_breach_entry_gate_while_hard_killswitch_stays_idle():
    """Executable demonstration of the divergence window the P1 work closes.

    equity=$10k, 5% cutoff = -$500 (tighter), USD floor -$2,000 (looser).
    At -$600 the unified ENTRY gate is already blocked, but the inline hard
    flatten — fed the same USD floor — does NOT flatten. Both current
    behaviours are asserted so the P1 fix must reconcile them deliberately.
    """
    from hermes_trader.agents import risk_gates

    equity, pnl = 10_000.0, -600.0
    cutoff, source = risk_gates.effective_daily_loss_cutoff(
        equity, max_daily_loss_usd=-2_000.0, daily_loss_pct=5.0)
    assert source == "pct" and cutoff == pytest.approx(-500.0)
    ctx = risk_gates.GateContext(
        confidence=0.9, current_positions=_positions("BTC"),
        trade_notional_usd=100.0, daily_pnl=pnl,
        market_volume_24h_usd=1e9, coin="BTC", trade_side="long",
        has_binary_news_risk=False, equity=equity, total_open_notional=100.0)
    gate = risk_gates.daily_loss_kill_switch(ctx, -2_000.0, 5.0)
    assert gate["pass"] is False  # entries blocked ...

    run = _load_killswitch_block()
    flattened = []
    run({"max_daily_loss_usd": -2_000.0}, equity, _positions("BTC"), pnl,
        flattener=lambda c: flattened.append(c) or {"ok": True}, events=[])
    assert flattened == []  # ... yet the losing book is not hard-flattened.


# ── B. Flattens ignore the LIVE entry grant; closes are reduce-only ────────

def _close_wire(monkeypatch, tmp_path):
    """Minimal real close_position_market harness: the function itself runs
    untouched, with only its I/O edges stubbed. Records every place_hl_order
    call so reduce_only/cloid can be asserted end to end."""
    from hermes_trader import notify
    from hermes_trader.agents import dsl_exit, executor

    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(tmp_path / "dsl.json"))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit.register_position("ETH", "long", 100.0, leverage=10)
    executor._CLOSE_SETTLED_AT.clear()

    monkeypatch.setattr(notify, "send_text", lambda *a, **kw: False)
    monkeypatch.setattr(notify, "send_card", lambda *a, **kw: False)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 94.0)
    monkeypatch.setattr(executor, "cancel_open_orders_for_coin", lambda c: None)

    live = {"ETH": {"szi": "1.0", "entryPx": "100"}}
    monkeypatch.setattr(
        executor, "fetch_account_state",
        lambda u, **kw: {"asset_positions": [
            {"position": {"coin": c, **p}} for c, p in live.items()]})

    orders = []

    def _fake_place(is_buy, size, mid_price, coin, **kw):
        orders.append({"is_buy": is_buy, "size": size, "coin": coin, **kw})
        live.pop(coin, None)
        return {"ok": True, "order_id": f"oid-{len(orders)}",
                "total_sz": float(size), "avg_px": 94.0}

    monkeypatch.setattr(executor, "place_hl_order", _fake_place)
    monkeypatch.setattr(executor.memory, "pop_entry_context", lambda *a, **kw: {})
    monkeypatch.setattr(executor.memory, "record_close", lambda *a, **kw: None)
    monkeypatch.setattr(executor.memory, "record_loss_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(executor.memory, "set_coin_circuit", lambda *a, **kw: None)
    monkeypatch.setattr(executor.memory, "set_global_halt", lambda *a, **kw: None)
    monkeypatch.setattr(executor.memory, "set_loss_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(executor.memory, "get_start_of_day_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "global_halt_remaining_min", lambda: 0.0)
    return executor, orders


def test_close_without_live_grant_is_reduce_only(monkeypatch, tmp_path):
    """Contract: HERMES_ENABLE_LIVE gates ENTRIES only. With the env var
    absent, close_position_market still reaches the exchange, and the order
    is reduce_only with a cloid idempotency key (executor.py:5717-5719)."""
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    executor, orders = _close_wire(monkeypatch, tmp_path)
    res = executor.close_position_market("ETH")
    assert res.get("ok") is True
    assert len(orders) == 1
    assert orders[0]["reduce_only"] is True
    assert orders[0]["cloid"] is not None


def test_close_source_never_calls_live_authorization():
    """The authorization symbol is imported for entries only; the close
    function bodies must not gain a live_trading_authorized() call (any of
    the five flatten paths funnels through close_position_market)."""
    tree = ast.parse((REPO_ROOT / "hermes_trader" / "agents"
                      / "executor.py").read_text(encoding="utf-8"))
    close_fn = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_close_position_market_locked")

    def _names(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    assert "live_trading_authorized" not in _names(close_fn)
    # and the close entry wrapper delegates straight to the locked path
    wrapper = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "close_position_market")
    assert "live_trading_authorized" not in _names(wrapper)


class _HaltMem:
    def __init__(self, remaining=30.0):
        self._remaining = remaining

    def global_halt_remaining_min(self):
        return self._remaining

    def coin_circuit_remaining_min(self, coin):
        return 0.0


def test_bm11_halt_flatten_through_real_close_without_live_grant(monkeypatch, tmp_path):
    """Breaker -> bm11_breaker_flatten -> real close_position_market ->
    place_hl_order(reduce_only=True) end to end, with no LIVE grant and a
    mode=OFF config in the file: flatten paths are exempt from both entry
    gates (the loop calls this block before its OFF continue)."""
    monkeypatch.delenv("HERMES_ENABLE_LIVE", raising=False)
    executor, orders = _close_wire(monkeypatch, tmp_path)
    events = []
    bm11 = _load_bm11(flattener=executor.close_position_market,
                      event_log=events.append)
    out = bm11(equity=1_000.0, positions=_positions("ETH"),
               cfg={"mode": "OFF", "auto_flatten_on_global_halt": True},
               mem=_HaltMem(30.0))
    assert out == {"ETH"}
    assert len(orders) == 1 and orders[0]["reduce_only"] is True
    assert any(e.get("event") == "global_halt_auto_flatten" for e in events)


# ── C. Tick ordering: exits before the OFF gate, entries after it ──────────

def _line_index(marker):
    idx = TRADING_LOOP_SRC.find(marker)
    assert idx != -1, f"anchor drifted: {marker!r}"
    return idx


def test_tick_order_flatten_paths_before_off_gate():
    """The OFF ``continue`` must remain AFTER every flatten/exit path so a
    mode=OFF deployment still hard-flattens on kill-switch / breakers / DSL
    stops / market circuit (trading_loop.py:1258 … :1657)."""
    ks = _line_index("_max_daily_loss = float(cfg_get(")
    bm11 = _line_index("bm11_breaker_flatten(equity, positions, _cfg, memory)")
    dsl = _line_index('_process_exits(exits, source="dsl")')
    mc = _line_index("market_circuit_tick(_cfg, memory, equity, positions)")
    off = _line_index('== "OFF":')
    assert ks < bm11 < dsl < mc < off
    # the OFF branch actually skips the rest of the cycle
    assert "exits still monitored" in TRADING_LOOP_SRC[off:off + 400]


def test_tick_order_scan_and_routing_after_off_gate():
    """scan_once / route_verdict (the ENTRY funnel) must remain AFTER the OFF
    gate, so mode=OFF never opens a position."""
    off = _line_index('== "OFF":')
    scan = _line_index("results = scan_once(")
    route = _line_index("routed = route_verdict(analysis)")
    assert off < scan < route


def test_bm11_and_market_circuit_default_flattener_is_real_close():
    """Source contract: both breaker helpers default their flattener to
    close_position_market (a lambda default in a test would silently
    unshackle the breaker from the real reduce-only close)."""
    tree = ast.parse(TRADING_LOOP_SRC)
    for name in ("bm11_breaker_flatten", "market_circuit_tick"):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == name)
        # bm11 binds it positionally; market_circuit_tick as a keyword default
        defaults = list(fn.args.defaults) + [d for d in fn.args.kw_defaults
                                             if d is not None]
        assert any(isinstance(a, ast.Name)
                   and a.id == "close_position_market" for a in defaults)
