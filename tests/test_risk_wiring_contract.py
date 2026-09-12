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
       * emits exactly one hard_killswitch event with the USD floor;
         ``flattened`` counts ACTUAL successes (never len(positions),
         which would report a failed flatten as a clean exit) and a
         per-coin close failure is mirrored to events.jsonl as an
         ``error`` event — the fund-safety guard never fails silently
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
import time
import types
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


def _load_market_circuit(*, evaluator, flattener, event_log):
    """Compile market_circuit_tick with injected keyword defaults; its
    internal wiring (verdict -> same-tick flatten -> event log) runs
    verbatim."""
    from hermes_trader.agents.config_store import cfg_get

    ns = {
        "cfg_get": cfg_get,
        "logger": logging.getLogger("test.contract.market_circuit"),
        "close_position_market": flattener,
        "market_circuit_evaluate": evaluator,
        "_market_circuit_funding": lambda c: None,
        "log_event": event_log,
    }
    exec(compile(_extract_function("market_circuit_tick"),
                 "<trading_loop extracted>", "exec"), ns)
    return ns["market_circuit_tick"]


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
                     "limit": -3.0, "flattened": 3, "failed": []}
    # all-success run emits no error events
    assert not [e for e in events if e.get("event") == "error"]


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
    # the summary counts ACTUAL successes and names the failure — it must
    # never report len(positions) as flattened when a close raised
    ev = next(e for e in events if e["event"] == "hard_killswitch")
    assert ev["flattened"] == 2
    assert ev["failed"] == ["ETH"]
    # the failed flatten is mirrored to events.jsonl, not only to logger
    err = [e for e in events if e.get("event") == "error"]
    assert len(err) == 1
    assert err[0]["scope"] == "hard_killswitch"
    assert err[0]["coin"] == "ETH"
    assert "exchange down" in err[0]["error"]


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


# ── E. Guard-flatten failures must reach events.jsonl, not only logger ─────

def test_bm11_global_halt_failed_flatten_is_recorded():
    """When a global-halt flatten raises for one coin, bm11 must (a) still
    flatten the sibling coins, (b) report an accurate flattened count with a
    ``failed`` list in the summary, and (c) mirror the failure as an
    ``error`` event so a guard that could not de-risk the book is visible in
    the authoritative outcome feed."""
    events = []
    done = []

    def _flattener(coin):
        if coin == "ETH":
            raise RuntimeError("exchange down")
        done.append(coin)
        return {"ok": True}

    bm11 = _load_bm11(flattener=_flattener, event_log=events.append)
    out = bm11(equity=1_000.0, positions=_positions("ETH", "BTC"),
               cfg={"auto_flatten_on_global_halt": True},
               mem=_HaltMem(30.0))
    assert out == {"BTC"} and done == ["BTC"]
    summary = next(e for e in events
                   if e["event"] == "global_halt_auto_flatten")
    assert summary["flattened"] == 1 and summary["failed"] == ["ETH"]
    err = [e for e in events if e.get("event") == "error"]
    assert len(err) == 1
    assert err[0]["scope"] == "bm11_global_halt"
    assert err[0]["coin"] == "ETH"
    assert "exchange down" in err[0]["error"]


class _CoinHaltMem:
    def global_halt_remaining_min(self):
        return 0.0

    def coin_circuit_remaining_min(self, coin):
        return 15.0 if coin == "ETH" else 0.0


def test_bm11_coin_circuit_failed_flatten_is_recorded():
    events = []

    def _flattener(coin):
        raise RuntimeError("exchange down")

    bm11 = _load_bm11(flattener=_flattener, event_log=events.append)
    out = bm11(equity=1_000.0, positions=_positions("ETH", "BTC"),
               cfg={"auto_flatten_on_coin_circuit": True},
               mem=_CoinHaltMem())
    assert out == set()
    summary = next(e for e in events
                   if e["event"] == "coin_circuit_auto_flatten")
    assert summary["failed"] == ["ETH"]
    err = next(e for e in events if e.get("event") == "error")
    assert err["scope"] == "bm11_coin_circuit"
    assert err["coin"] == "ETH"


def test_market_circuit_failed_flatten_is_recorded():
    """market_circuit_tick's same-tick enforce flatten must mirror a failing
    close as an ``error`` event (market_circuit scope)."""
    events = []

    def _evaluator(cfg, *, mem, funding_fetcher, notifier, event_log):
        return {"action": "halt_armed"}

    def _flattener(coin):
        raise RuntimeError("exchange down")

    tick = _load_market_circuit(evaluator=_evaluator, flattener=_flattener,
                                event_log=events.append)
    tick({"market_circuit": {"mode": "enforce"},
          "auto_flatten_on_global_halt": True},
         _HaltMem(30.0), 1_000.0, _positions("ETH"))
    err = next(e for e in events if e.get("event") == "error")
    assert err["scope"] == "market_circuit"
    assert err["coin"] == "ETH"
    assert "exchange down" in err["error"]


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


# ── F. Executor close-path failures must reach events.jsonl ────────────────

def test_close_partial_fill_is_durably_recorded(monkeypatch, tmp_path):
    """A reduce-only close that leaves a residual (follow-up also partial)
    must write a ``close_partial`` event to events.jsonl in addition to the
    log+alert, so an un-flattened residual is reconstructible post-trade
    (executor.py partial-fill guard ~:5764-5794)."""
    from hermes_trader import event_log
    executor, _orders = _close_wire(monkeypatch, tmp_path)

    written = []
    monkeypatch.setattr(event_log, "append",
                        lambda event, payload=None, **kw: written.append(
                            {"event": event, "payload": payload or {}}) or True)

    def _partial_place(is_buy, size, mid_price, coin, **kw):
        # primary fills 0.5 of 1.0; follow-up fills 0.1 of the 0.5 residual
        filled = 0.5 if size >= 1.0 else 0.1
        return {"ok": True, "total_sz": filled, "avg_px": 94.0}

    monkeypatch.setattr(executor, "place_hl_order", _partial_place)
    res = executor.close_position_market("ETH")
    assert res.get("partial") is True and res.get("residual_sz") == 0.4
    ev = next(e for e in written if e["event"] == "close_partial")
    assert ev["payload"]["coin"] == "ETH"
    assert ev["payload"]["residual_sz"] == 0.4
    assert ev["payload"]["requested_sz"] == 1.0


def test_record_close_failure_is_durably_recorded(monkeypatch, tmp_path):
    """When memory.record_close raises (a close row would be lost from the
    outcome store), the close must still settle and an ``error`` event must
    land in events.jsonl — previously this existed only in logger+notify
    (executor.py ~:5942-5958)."""
    from hermes_trader import event_log
    executor, orders = _close_wire(monkeypatch, tmp_path)

    written = []
    monkeypatch.setattr(event_log, "append",
                        lambda event, payload=None, **kw: written.append(
                            {"event": event, "payload": payload or {}}) or True)

    def _boom(*a, **kw):
        raise RuntimeError("outcome store down")

    monkeypatch.setattr(executor.memory, "record_close", _boom)
    res = executor.close_position_market("ETH")
    # the fill itself is unaffected — the reduce-only order still went out
    assert res.get("ok") is True and len(orders) == 1
    err = next(e for e in written if e["event"] == "error")
    assert err["payload"]["scope"] == "outcome_store"
    assert err["payload"]["coin"] == "ETH"
    assert "outcome store down" in err["payload"]["error"]


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


# ── G. Non-guard state-consistency failures must also reach events.jsonl ───
# Q1 exception-hygiene batch 2: these paths are not fund-safety flatten
# guards, but a silent failure still disables a protection (anti-revenge
# cooldown) or loses post-trade reconstruction data (perception feed, entry
# attribution, orphan-window rehydrate). Each must mirror the failure to the
# authoritative event feed as an ``error`` event, best-effort and without
# changing any control flow / trading behaviour.

def _main_loop_try(predicate):
    """Find the unique Try node inside the module-level main loop for which
    ``predicate`` holds on the node, and return a runner that execs the Try
    (plus, when it is guarded by an enclosing If in the same suite, that If)
    verbatim in an injected namespace."""
    tree = ast.parse(TRADING_LOOP_SRC)
    loop = next(n for n in tree.body if isinstance(n, ast.While))
    matches = [(n, p) for n, p in _iter_with_parent(loop)
               if isinstance(n, ast.Try) and predicate(n)]
    assert len(matches) == 1, f"expected exactly one matching Try, got {len(matches)}"
    node, holder = matches[0]
    # When the Try is the sole body of an immediate enclosing If in the same
    # statement suite (e.g. the ``if _net_usd < 0:`` cooldown guard), compile
    # the If alone: its body already executes the Try, so adding the Try as a
    # sibling module statement would run it twice.
    top = holder if (isinstance(holder, ast.If)
                     and holder.body == [node]) else node
    block = ast.fix_missing_locations(
        ast.Module(body=[top], type_ignores=[]))

    def _run(ns):
        base = {"logger": logging.getLogger("test.contract.g"),
                "log_event": ns.pop("log_event")}
        base.update(ns)
        exec(compile(block, "<trading_loop g extracted>", "exec"), base)

    return _run


def test_perception_persist_failure_is_recorded():
    """A failing memory.record_perception (per-coin, per-tick signal feed)
    must not vanish into ``except: pass``: mirror it as an ``error`` event so
    a broken perception/outcome store is observable, while never blocking the
    scan (trading_loop.py ~:1837-1840)."""
    events = []

    def _boom(_perception):
        raise RuntimeError("memory disk full")

    class _Mem:
        record_perception = staticmethod(_boom)

    run = _main_loop_try(
        lambda n: any(isinstance(s, ast.Expr)
                      and isinstance(s.value, ast.Call)
                      and isinstance(s.value.func, ast.Attribute)
                      and s.value.func.attr == "record_perception"
                      for s in n.body))
    run({"memory": _Mem(),
         "perception": {"coin": "BTC", "composite_score": 1},
         "coin": "BTC",
         "log_event": events.append})
    err = [e for e in events if e.get("event") == "error"]
    assert len(err) == 1
    assert err[0]["scope"] == "perception_persist"
    assert err[0]["coin"] == "BTC"
    assert "memory disk full" in err[0]["error"]


def test_loss_cooldown_arm_failure_is_recorded():
    """When the anti-revenge loss cooldown cannot be armed after an
    exchange-triggered losing close, a warning-only handler would silently
    leave re-entries unguarded. It must also emit an ``error`` event scoped
    ``loss_cooldown`` (trading_loop.py ~:1484-1500)."""
    events = []

    def _boom(_coin, _until):
        raise RuntimeError("cooldown store down")

    class _Mem:
        set_loss_cooldown = staticmethod(_boom)

    run = _main_loop_try(
        lambda n: any(
            "loss-cooldown arm failed" in (
                ast.get_source_segment(TRADING_LOOP_SRC, h) or "")
            for h in n.handlers))
    _tr = types.SimpleNamespace(coin="ETH")
    run({
        "_tr": _tr, "_net_usd": -5.0,
        "cfg_get": lambda *a, **k: 30.0,
        "read_agent_config": lambda: {},
        "time": time,
        "memory": _Mem(),
        "log_event": events.append,
    })
    err = [e for e in events if e.get("event") == "error"]
    assert len(err) == 1
    assert err[0]["scope"] == "loss_cooldown"
    assert err[0]["coin"] == "ETH"
    assert "cooldown store down" in err[0]["error"]


def test_entry_context_capture_failure_is_recorded(monkeypatch):
    """memory.record_entry_context feeds post-trade attribution (regime /
    config-era / enforcement snapshot). A failure was logged at DEBUG only;
    it must reach events.jsonl as an ``error`` scoped ``entry_context`` while
    the fill itself still settles (executor.py ~:2856-2873)."""
    from hermes_trader.agents import executor
    from hermes_trader.agents import market_regime
    from hermes_trader import event_log

    monkeypatch.setattr(executor, "register_position",
                        lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "record_trade", lambda t: None)
    monkeypatch.setattr(market_regime, "detect_regime",
                        lambda coin, *, force=False: "neutral")

    def _boom(*a, **k):
        raise RuntimeError("context store down")

    monkeypatch.setattr(executor.memory, "record_entry_context", _boom)

    written = []
    monkeypatch.setattr(event_log, "append",
                        lambda event, payload=None, **kw: written.append(
                            {"event": event, "payload": payload or {}}) or True)

    executor._register_filled_position(
        analysis={"id": "a1", "coin": "BTC"}, config={},
        order_res={"avg_px": 100.0, "total_sz": 1.0, "order_id": "O1"},
        coin="BTC", trade_side="long", mid_price=100.0, size_in_coin=1.0,
        atr=2.0, leverage=10, user="0xUSER", override_composite=0.0,
        enf=None, aid="a1")

    err = [e for e in written if e["event"] == "error"]
    assert len(err) == 1
    assert err[0]["payload"]["scope"] == "entry_context"
    assert err[0]["payload"]["coin"] == "BTC"
    assert "context store down" in err[0]["payload"]["error"]


def test_h6_rehydrate_failure_is_recorded(monkeypatch):
    """On an UNRESOLVABLE response-unknown order, the immediate rehydrate that
    shrinks the orphan window can itself fail. That failure was only logged;
    it must land in events.jsonl as an ``error`` scoped ``h6_rehydrate`` while
    the streak/halt control flow is untouched (executor.py ~:2526-2537)."""
    from hermes_trader.agents import executor
    from hermes_trader.client import exchange
    from hermes_trader.agents import dsl_exit
    from hermes_trader import event_log

    monkeypatch.setattr(exchange, "reconcile_order_fill",
                        lambda **k: {"status": "unknown",
                                     "reason": "userFills_fetch_exception: boom"})
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda u, **kw: {"asset_positions": []})

    def _boom_rehydrate(*a, **k):
        raise RuntimeError("rehydrate exploded")

    monkeypatch.setattr(dsl_exit, "rehydrate_from_exchange", _boom_rehydrate)
    monkeypatch.setattr(executor.memory, "set_global_halt", lambda until: None)

    written = []
    monkeypatch.setattr(event_log, "append",
                        lambda event, payload=None, **kw: written.append(
                            {"event": event, "payload": payload or {}}) or True)

    executor._reset_resp_unknown_streak()
    out = executor._reconcile_unknown_order_result(
        {"ok": False, "error": "conn reset", "error_code": "response_unknown"},
        coin="BTC", is_buy=True, size_in_coin=1.0, mid_price=100.0,
        cloid="0xcloid", config={"leverage": 10}, user="0xUSER",
        mode="LIVE", aid="a1", gate_results=[])
    assert out["executed"] is False
    assert "order_response_unknown_unresolved" in out["reason"]
    err = [e for e in written if e["event"] == "error"]
    assert len(err) == 1
    assert err[0]["payload"]["scope"] == "h6_rehydrate"
    assert err[0]["payload"]["coin"] == "BTC"
    assert "rehydrate exploded" in err[0]["payload"]["error"]
