"""Deep-audit (2026-08-28) P1 remediation tests.

Covers four P1 items:

* A-F4 — concurrent same-coin double-open race. A fresh analysis_id is
  minted every scan cycle, so two DIFFERENT analyses for the same coin are
  invisible to the analysis-id in-flight set. A coin-dimension in-flight set
  (``_IN_FLIGHT_COINS``) blocks the second caller, and a fresh live position
  re-check immediately before place_hl_order refuses to double-open when the
  coin already has a position (fail-open on read failure).

* A-F5 — DSL must not exit on a single-book wick. Floor-breach exits now
  require the position to be held for ``breach_confirm_sec`` (default 4.0s)
  AND, when an oracle/index price is available, require the INDEX to also be
  through the floor. A wick moves the mid but not the oracle blend; a missing
  index degrades to mid+time gating (exchange-side backup SL is the net).

* B-M11 — breakers (global halt / per-coin circuit) only blocked new
  entries; they never flattened. Two opt-in switches
  (``auto_flatten_on_global_halt`` / ``auto_flatten_on_coin_circuit``, BOTH
  DEFAULT OFF) now flat-all / flat-that-coin via close_position_market and log
  the action. Default-off preserves the legacy "stop adding risk" contract.

* A-F14 — no live candle/feed freshness gate. This batch delivers the REST
  mid-feed freshness door: a successful main-book all_mids fetch is stamped;
  ``mid_feed_age_seconds()`` exposes its age; the trading loop pauses entries
  on empty/blind snapshots AND pauses BOTH entries and DSL exits when the
  feed is older than ``MID_FEED_MAX_STALE_S`` (candle WS + gap backfill is
  tracked separately).
"""

import ast
import logging
import os
import time
from pathlib import Path

import pytest


# ── shared helpers ────────────────────────────────────────────────────────

def _isolated_memory(monkeypatch, tmp_path):
    """AgentMemory pointed at tmp paths and installed as the module singleton."""
    from hermes_trader.agents import memory as memory_mod
    import hermes_trader.event_log as event_log
    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    events_path = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", events_path)
    monkeypatch.setattr(event_log, "EVENTS_FILE", events_path)
    m = memory_mod.AgentMemory()
    m.load()
    monkeypatch.setattr(memory_mod, "memory", m)
    return m


# Permissive LIVE config: every registered gate passes; only the A-F4 coin /
# pre-place logic can block. Legacy sizing path (atr_risk_sizing off).
_LIVE_CFG = {
    "mode": "LIVE", "enable_crypto": True,
    "leverage": 10,
    "equity_fraction_per_trade": 0.2,
    "max_trade_notional_usd": 0,
    "max_concurrent": 9999,
    "min_market_volume_usd": 0,
    "min_hip3_volume_usd": 0,
    "min_short_volume_usd": 0,
    "max_total_notional_pct": 100_000.0,
    "max_daily_loss_usd": -1_000_000_000,
    "min_ai_confidence": 0.0,
    "aligned_min_conf": None,
    "min_trend_score": 0.0,
    "coin_allowlist": [],
    "coin_blocklist": [],
    "max_crypto_long_correlated": 9999,
    "cooldown_min": 0,
    "daily_giveback_halt_pct": 0,
    "daily_giveback_min_peak_usd": 0,
    "counter_regime_min_conf": 0.0,
    "block_counter_trend_bypass": False,
    "crowded_with_min_conf": 0.0,
    "debate_gate": {"enabled": False},
    "news_blackout": {"enabled": False},
    "circuit_breaker": {"consecutive_loss_limit": 0,
                        "coin_daily_loss_pct": 0.0,
                        "max_drawdown_pct": 0.0},
    "max_atr_pct": 15.0,
    "max_spread_pct": 1.0,
}

_ANALYSIS = {
    "id": "p1test", "coin": "ETH", "action": "LONG", "side": "long",
    "confidence": 0.9, "composite_score": 80,
    "entry_px": 100.0, "stop_px": 95.0, "tp_px": 110.0,
    "reasoning": "p1 deep-path test",
}


@pytest.fixture(autouse=True)
def _clean_p1_state(monkeypatch):
    """Reset every process-wide marker / cache the P1 code touches, and set
    the env a LIVE deep-path run needs (private key; liq buffer gate off so
    its extra fetch never enters the A-F4 fake dispatch)."""
    from hermes_trader.agents import executor, dsl_exit
    from hermes_trader.client import exchange
    executor._IN_FLIGHT_ANALYSES.clear()
    executor._IN_FLIGHT_COINS.clear()
    dsl_exit._active_positions.clear()
    dsl_exit._IDX_CACHE.clear()
    with exchange._MID_FEED_LOCK:
        exchange._MID_FEED_LAST_OK_MONO = 0.0
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("HERMES_LIQ_BUFFER_USD", "0")
    yield
    executor._IN_FLIGHT_ANALYSES.clear()
    executor._IN_FLIGHT_COINS.clear()
    dsl_exit._active_positions.clear()
    dsl_exit._IDX_CACHE.clear()


def _stub_deep_path(monkeypatch, fetch_fake=None, place_fake=None):
    """Wire every network / external touchpoint in the LIVE open path.

    ``fetch_fake`` dispatches on kwargs: the initial read calls
    fetch_account_state(user, include_hip3=True); the A-F4 pre-place re-check
    calls fetch_account_state(user) with no kwargs.
    """
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(_LIVE_CFG))
    monkeypatch.setattr(executor, "get_max_leverage", lambda _c: 50)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    if fetch_fake is None:
        def fetch_fake(*_a, **_k):
            return {"equity": 1000.0, "available": 900.0,
                    "total_ntl": 0.0, "asset_positions": []}
    monkeypatch.setattr(executor, "fetch_account_state", fetch_fake)
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 10.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
    # Gate spy: permit everything (gate-level coverage lives in H4/H6 tests).
    monkeypatch.setattr(executor, "eval_all_gates",
                        lambda ctx, config, *a, **k: {
                            "blocked": False, "block_reasons": [], "results": {}})
    # Network touchpoints reached after the gates but before claim.
    monkeypatch.setattr(executor, "set_leverage", lambda *_a, **_k: None)

    def _spread(_coin):
        return {"ok": True, "spread_pct": 0.01, "best_bid": 99.99,
                "best_ask": 100.01, "bid_depth_1pct_usd": 1e6,
                "ask_depth_1pct_usd": 1e6, "error": None}
    monkeypatch.setattr(executor, "get_orderbook_spread", _spread)
    place_calls = []
    if place_fake is None:
        def place_fake(*a, **k):
            place_calls.append((a, k))
            return {"ok": False, "error": "forced"}
    monkeypatch.setattr(executor, "place_hl_order", place_fake)
    return place_calls


# ── A-F4: coin-dimension idempotency ─────────────────────────────────────

def test_af4_coin_in_flight_blocks_concurrent_same_coin(monkeypatch, tmp_path):
    """A different analysis_id targeting a coin already mid-order is blocked
    by the coin set BEFORE the gates' snapshot is trusted; no order placed."""
    from hermes_trader.agents import executor
    _isolated_memory(monkeypatch, tmp_path)
    place_calls = _stub_deep_path(monkeypatch)

    # Simulate another caller already placing ETH (distinct analysis_id).
    executor._IN_FLIGHT_COINS.add("ETH")

    analysis = dict(_ANALYSIS, id="af4-coinflight")
    result = executor.maybe_execute(analysis)

    assert result.get("executed") is False
    assert result.get("reason") == "coin_order_in_flight"
    assert place_calls == []
    # The coin marker belonging to the OTHER caller must not be dropped.
    assert "ETH" in executor._IN_FLIGHT_COINS
    assert "af4-coinflight" not in executor._IN_FLIGHT_ANALYSES


def test_af4_pre_place_recheck_blocks_double_open(monkeypatch, tmp_path):
    """Gates ran against an empty-positions snapshot; the fresh re-check right
    before placing shows the coin now has a live position → refuse to place,
    clear BOTH markers (ours), and surface the distinct reason."""
    from hermes_trader.agents import executor
    _isolated_memory(monkeypatch, tmp_path)

    def fetch_fake(*_a, **kwargs):
        if kwargs.get("include_hip3"):
            # Initial pipeline read: no positions.
            return {"equity": 1000.0, "available": 900.0,
                    "total_ntl": 0.0, "asset_positions": []}
        # A-F4 pre-place re-check: another caller's fill landed.
        return {"equity": 1000.0, "asset_positions": [
            {"position": {"coin": "ETH", "szi": 0.5}}]}

    place_calls = _stub_deep_path(monkeypatch, fetch_fake=fetch_fake)

    analysis = dict(_ANALYSIS, id="af4-recheck")
    result = executor.maybe_execute(analysis)

    assert result.get("executed") is False
    assert result.get("reason") == "position_already_open_pre_place"
    assert place_calls == []
    # Markers claimed by THIS call must be discarded.
    assert "ETH" not in executor._IN_FLIGHT_COINS
    assert "af4-recheck" not in executor._IN_FLIGHT_ANALYSES


def test_af4_pre_place_recheck_fail_open_then_order_failed(monkeypatch, tmp_path):
    """A re-check READ failure must not block trading (fail-open): placement
    proceeds; a definite order failure then clears both markers."""
    from hermes_trader.agents import executor
    _isolated_memory(monkeypatch, tmp_path)

    def fetch_fake(*_a, **kwargs):
        if kwargs.get("include_hip3"):
            return {"equity": 1000.0, "available": 900.0,
                    "total_ntl": 0.0, "asset_positions": []}
        raise RuntimeError("boom")

    place_calls = _stub_deep_path(monkeypatch, fetch_fake=fetch_fake)

    analysis = dict(_ANALYSIS, id="af4-failopen")
    result = executor.maybe_execute(analysis)

    # Re-check raised → fail-open → place reached; our fake returns a definite
    # failure, so the order_failed branch (NOT the H6 reconcile) handles it.
    assert len(place_calls) == 1
    assert result.get("executed") is False
    assert result.get("reason") == "order_failed: forced"
    # No marker leak on the failure path.
    assert "ETH" not in executor._IN_FLIGHT_COINS
    assert "af4-failopen" not in executor._IN_FLIGHT_ANALYSES


# ── A-F5: breach time gate default + index wick confirmation ─────────────

def test_af5_breach_confirm_sec_defaults_to_4s():
    """The wick confirmation window defaults to 4.0s everywhere: dataclass,
    canonical config block, and cfg_get with no user config."""
    from hermes_trader.agents.dsl_exit import ExitPolicy
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get
    assert ExitPolicy().breach_confirm_sec == 4.0
    assert CANONICAL_DEFAULTS["dsl_exit"]["breach_confirm_sec"] == 4.0
    assert cfg_get("dsl_exit.breach_confirm_sec", config={}) == 4.0


def _wick_policy():
    # confirm_sec=0 isolates the INDEX gate from the time gate (a first breach
    # tick with confirm_sec=4.0 has elapsed≈0 and never exits, by design).
    from hermes_trader.agents.dsl_exit import ExitPolicy
    return ExitPolicy(breach_confirm_sec=0.0, protect_pct=1.25,
                      retrace_threshold=0.20, max_loss_pct=100.0,
                      max_loss_roe_pct=1000.0)


def test_af5_index_above_floor_suppresses_wick_exit(monkeypatch):
    """Mark pokes through the trailing floor but the INDEX stays above:
    classic single-book wick → hold (no exit), and the breach run is NOT
    reset (the next confirming tick must fire)."""
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(dsl_exit, "_request_save", lambda **_k: None)
    t = dsl_exit.DSLTracker("ETH", "long", 100.0, time.time(),
                            policy=_wick_policy())
    # Tick 1: +2% → phase-2 armed; floor = 100 + 2*0.8 = 101.6.
    assert t.check(102.0).exit is False
    # Tick 2: mark 101.5 breaches 101.6, but index 101.7 holds above floor.
    v = t.check(101.5, index_px=101.7)
    assert v.exit is False
    assert t.consecutive_breaches == 1  # count retained, not reset
    # Tick 3: SAME tracker — index now also through the floor → exit fires.
    v2 = t.check(101.5, index_px=101.4)
    assert v2.exit is True
    assert "idx-confirmed" in v2.reason


def test_af5_missing_index_degrades_to_mid_gate(monkeypatch):
    """No usable index (None) → the exit degrades to mid/time gating so a
    real breach still closes (exchange backup SL is the ultimate net)."""
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(dsl_exit, "_request_save", lambda **_k: None)
    t = dsl_exit.DSLTracker("ETH", "long", 100.0, time.time(),
                            policy=_wick_policy())
    assert t.check(102.0).exit is False
    v = t.check(101.5, index_px=None)
    assert v.exit is True
    assert "idx-confirmed" not in v.reason


def test_af5_get_index_prices_cache_failure_and_hip3(monkeypatch):
    """get_index_prices: parses metaAndAssetCtxs, caches for _IDX_CACHE_TTL_S
    (one POST per dex per pass), returns {} on POST failure, and maps HIP-3
    coins via the dex payload / colon-namespaced result key."""
    import hermes_trader.client.hl_client as hl_client
    from hermes_trader.agents import dsl_exit

    calls = []

    def fake_post(path, payload, timeout=None):
        calls.append({"path": path, "payload": payload})
        if payload.get("dex") == "xyz":
            return [{"universe": [{"name": "MU"}]},
                    [{"oraclePx": "200.5"}]]
        return [{"universe": [{"name": "ETH"}, {"name": "BTC"}]},
                [{"oraclePx": "100.5"}, {"oraclePx": "50000"}]]

    monkeypatch.setattr(hl_client, "_http_post", fake_post)

    # Native coins parse + cache.
    r1 = dsl_exit.get_index_prices({"ETH", "BTC"})
    assert r1 == {"ETH": 100.5, "BTC": 50000.0}
    r2 = dsl_exit.get_index_prices({"ETH"})  # TTL window → cache hit
    assert r2 == {"ETH": 100.5}
    main_posts = [c for c in calls if not c["payload"].get("dex")]
    assert len(main_posts) == 1  # second call served from cache

    # HIP-3 coin: dex payload + colon-namespaced key.
    calls.clear()
    r3 = dsl_exit.get_index_prices({"xyz:MU"})
    assert r3 == {"xyz:MU": 200.5}
    hip3_post = next(c for c in calls if c["payload"].get("dex") == "xyz")
    assert hip3_post["payload"]["type"] == "metaAndAssetCtxs"

    # POST failure (after cache expiry) → empty dict, no raise.
    dsl_exit._IDX_CACHE.clear()

    def _boom(*_a, **_k):
        raise RuntimeError("network down")
    monkeypatch.setattr(hl_client, "_http_post", _boom)
    assert dsl_exit.get_index_prices({"ETH"}) == {}


# ── B-M11: opt-in auto-flatten on breakers (helpers lifted from the loop) ─

def _load_loop_fn(name):
    """Extract a module-level FunctionDef from scripts/trading_loop.py and
    exec it in a controlled namespace — importing the module runs its
    module-level ``while True`` loop, so the loop logic is tested as pure
    functions (the helpers were factored out for exactly this reason)."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "scripts" / "trading_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == name)
    from hermes_trader.agents.config_store import cfg_get
    ns = {
        "cfg_get": cfg_get,
        "logger": logging.getLogger("test.bm11"),
        # Default args bound at def-execution time.
        "close_position_market": lambda *_a, **_k: {"ok": True},
        "log_event": lambda *_a, **_k: None,
        "MID_FEED_MAX_STALE_S": 30.0,
    }
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    exec(compile(module, "<trading_loop extracted>", "exec"), ns)
    return ns[name]


class _FakeMem:
    def __init__(self, global_min=0.0, coin_min=None, boom=False):
        self._g = global_min
        self._c = coin_min or {}
        self._boom = boom

    def global_halt_remaining_min(self):
        if self._boom:
            raise RuntimeError("mem read failed")
        return self._g

    def coin_circuit_remaining_min(self, coin):
        if self._boom:
            raise RuntimeError("mem read failed")
        return self._c.get(coin, 0.0)


def test_bm11_switches_default_off_even_when_halt_armed():
    """Both switches DEFAULT OFF: an armed global halt flattens nothing."""
    fn = _load_loop_fn("bm11_breaker_flatten")
    flattened = []
    events = []
    out = fn(equity=1000.0,
             positions=[{"position": {"coin": "ETH"}}],
             cfg={}, mem=_FakeMem(global_min=30.0),
             flattener=lambda c: flattened.append(c) or {"ok": True},
             event_log=lambda e: events.append(e))
    assert out == set()
    assert flattened == [] and events == []


def test_bm11_no_equity_or_no_positions_is_a_noop():
    """A degraded equity read (0) or an empty book can never flatten."""
    fn = _load_loop_fn("bm11_breaker_flatten")
    cfg = {"auto_flatten_on_global_halt": True,
           "auto_flatten_on_coin_circuit": True}
    flat = []
    assert fn(0.0, [{"position": {"coin": "ETH"}}], cfg,
             _FakeMem(global_min=30.0), flattener=lambda c: flat.append(c)) == set()
    assert fn(1000.0, [], cfg, _FakeMem(global_min=30.0),
             flattener=lambda c: flat.append(c)) == set()
    assert flat == []


def test_bm11_global_halt_flattens_every_coin_and_logs():
    """Global switch ON + halt armed → close EVERY held coin + one event."""
    fn = _load_loop_fn("bm11_breaker_flatten")
    flattened = []
    events = []
    out = fn(1000.0,
             [{"position": {"coin": "ETH"}}, {"position": {"coin": "BTC"}}],
             {"auto_flatten_on_global_halt": True},
             _FakeMem(global_min=42.0),
             flattener=lambda c: flattened.append(c) or {"ok": True},
             event_log=lambda e: events.append(e))
    assert set(flattened) == {"ETH", "BTC"}
    assert out == {"ETH", "BTC"}
    ev = next(e for e in events if e["event"] == "global_halt_auto_flatten")
    assert ev["flattened"] == 2 and ev["remaining_min"] == 42.0


def test_bm11_coin_circuit_flattens_only_that_coin():
    """Coin switch ON + circuit armed on ONE coin → only that coin flattens,
    with a coin-specific event."""
    fn = _load_loop_fn("bm11_breaker_flatten")
    flattened = []
    events = []
    out = fn(1000.0,
             [{"position": {"coin": "ETH"}}, {"position": {"coin": "BTC"}}],
             {"auto_flatten_on_coin_circuit": True},
             _FakeMem(coin_min={"ETH": 15.0, "BTC": 0.0}),
             flattener=lambda c: flattened.append(c) or {"ok": True},
             event_log=lambda e: events.append(e))
    assert flattened == ["ETH"]
    assert out == {"ETH"}
    ev = next(e for e in events if e["event"] == "coin_circuit_auto_flatten")
    assert ev["coin"] == "ETH" and ev["remaining_min"] == 15.0


def test_bm11_global_pass_coins_not_reflattened_by_coin_pass():
    """A coin closed by the global pass is skipped by the coin pass (one
    close per coin per tick)."""
    fn = _load_loop_fn("bm11_breaker_flatten")
    flattened = []
    out = fn(1000.0, [{"position": {"coin": "ETH"}}],
             {"auto_flatten_on_global_halt": True,
              "auto_flatten_on_coin_circuit": True},
             _FakeMem(global_min=30.0, coin_min={"ETH": 12.0}),
             flattener=lambda c: flattened.append(c) or {"ok": True},
             event_log=lambda e: None)
    assert flattened == ["ETH"]
    assert out == {"ETH"}


def test_bm11_state_and_flattener_failures_never_raise():
    """A breaker-state read that raises is treated as "not armed"; a flattener
    that raises for one coin is caught and never crashes the tick."""
    fn = _load_loop_fn("bm11_breaker_flatten")

    def _boom_flattener(c):
        if c == "ETH":
            raise RuntimeError("exchange down")
        return {"ok": True}

    # State read raises → nothing flattens, no exception escapes.
    assert fn(1000.0, [{"position": {"coin": "ETH"}}],
              {"auto_flatten_on_global_halt": True},
              _FakeMem(boom=True), flattener=_boom_flattener) == set()
    # Flattener raises for ETH but BTC still flattens; ETH not in the set.
    out = fn(1000.0,
             [{"position": {"coin": "ETH"}}, {"position": {"coin": "BTC"}}],
             {"auto_flatten_on_global_halt": True},
             _FakeMem(global_min=30.0),
             flattener=_boom_flattener, event_log=lambda e: None)
    assert out == {"BTC"}


# ── A-F14: REST mid-feed freshness door ──────────────────────────────────

def test_af14_feed_decision_four_branches():
    """The pure per-tick feed decision:
      empty mids          → entries paused, exits still run
      stale past budget   → entries AND DSL exits paused (skip_exits=True)
      blind held coin     → entries paused, exits still run
      healthy             → no halt"""
    fn = _load_loop_fn("af14_feed_decision")
    reason, skip_exits = fn(mids={}, stale_age=None, missing_mids=set())
    assert "empty mids snapshot" in reason and skip_exits is False

    reason, skip_exits = fn(mids={"ETH": 1.0}, stale_age=31.0,
                            missing_mids=set())
    assert "stale" in reason and skip_exits is True

    reason, skip_exits = fn(mids={"ETH": 1.0}, stale_age=1.0,
                            missing_mids={"BTC"})
    assert "no usable mid" in reason and "BTC" in reason and skip_exits is False

    reason, skip_exits = fn(mids={"ETH": 1.0}, stale_age=1.0,
                            missing_mids=set())
    assert reason is None and skip_exits is False


def test_af14_stale_budget_constant():
    from hermes_trader.client.exchange import MID_FEED_MAX_STALE_S
    assert MID_FEED_MAX_STALE_S == 30.0


def test_af14_mid_feed_stamp_and_age(monkeypatch):
    """mid_feed_age_seconds is None at cold start; a non-empty main-book
    fetch stamps it; an EMPTY fetch does NOT (empty = degraded read, not
    proof of liveness)."""
    from hermes_trader.client import exchange

    class _Info:
        def __init__(self, mids):
            self._mids = mids

        def all_mids(self, *a, **k):
            return dict(self._mids)

    # Cold start → unknown (not stale).
    assert exchange.mid_feed_age_seconds() is None

    # Healthy non-empty fetch → age becomes measurable.
    monkeypatch.setattr(exchange, "_get_info", lambda: _Info({"ETH": "100"}))
    exchange.get_all_hl_mids()
    age = exchange.mid_feed_age_seconds()
    assert age is not None and age >= 0.0

    # Empty degraded read → stamp NOT refreshed; reset + empty stays None.
    with exchange._MID_FEED_LOCK:
        exchange._MID_FEED_LAST_OK_MONO = 0.0
    monkeypatch.setattr(exchange, "_get_info", lambda: _Info({}))
    exchange.get_all_hl_mids()
    assert exchange.mid_feed_age_seconds() is None
