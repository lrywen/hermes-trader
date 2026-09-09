"""E5 (P2) state-resilience regressions.

Covers four state-robustness defects in the DSL tracker registry and the
agent-memory cache, plus two small wiring/default fixes:

  1. Corrupt state isolation: when ``.dsl-state.json`` / ``.agent-memory.json``
     is unreadable/undecodable it must be quarantined to a ``.corrupt-<ts>``
     sidecar (never silently truncated), a Feishu risk card fired, a metric
     counter bumped, and -- for DSL -- the previous ``.bak`` rolled forward so
     the ratchet is recovered instead of rebuilt from scratch.
  2. Single-generation ``.bak`` rotation on every successful state/memory
     write, so a future corruption has a healthy predecessor to fall back to.
  3. Rehydrate floor seed: a tracker synthesized after a state wipe inherits a
     CONSERVATIVE trailing-floor seed derived from the exchange bracket SL
     trigger price, so the monotonic ratchet cannot snap to its loosest value
     (the "ratchet reset to zero" defect).
  4. Conservative fill-time fallback: when userFills cannot resolve the real
     entry time, the synthetic tracker is aged CONSERVATIVELY (treated as an
     over-age hard_timeout candidate) instead of being stamped ``now``, which
     would reset the timeout clock for a zombie position.
  5. ``_tracker_from_dict`` defaults: a persisted policy missing
     ``stale_flat_timeout_minutes`` hydrates to the canonical 480 min (not 0),
     and a dirty/non-sensical ``last_floor`` (0 for a long, etc.) is dropped to
     None instead of poisoning the ratchet.
  6. Executor registration propagates ``hard_stop_confirm_sec`` from config
     into the ExitPolicy (previously the kwarg was dropped → the hard-stop
     wick guard silently reverted to its dataclass default).

All resilience paths are best-effort and must NEVER raise into the trading
hot path; these tests assert non-raising behaviour above all.
"""
from __future__ import annotations

import glob
import json
import os

import pytest

from hermes_trader.agents import dsl_exit
from hermes_trader.agents import market_regime
from hermes_trader import metrics

# Minimal dsl_exit config used by the rehydrate synth tests.
DSL_CFG = {
    "protect_pct": 1.5,
    "retrace_threshold": 0.35,
    "max_loss_pct": 1.0,
    "max_loss_roe_pct": 15.0,
}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": DSL_CFG})
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE", None)
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE_TS", 0.0)
    # Never let a real Feishu card escape the sandbox; individual tests spy on
    # the same attribute to assert the alert fired.
    monkeypatch.setattr(
        "hermes_trader.notify.send_card", lambda *a, **k: None)
    yield
    # Teardown: _isolate() points persistence at a tmp file and populates the
    # module-level _active_positions; without clearing it here, positions leak
    # into later alphabetically-ordered test files (server.py's re-entry
    # backstop merges active_position_coins() into flattened account state).
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False


def _isolate(monkeypatch, tmp_path):
    """Point DSL persistence at a tmp file and reset in-memory state."""
    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False
    return dsl_exit, state_file


def _positions(*coins_szi):
    out = []
    for coin, szi, entry in coins_szi:
        out.append({"position": {"coin": coin, "szi": str(szi), "entryPx": str(entry)}})
    return out


def _counter_value(counter):
    try:
        return float(counter._value.get())
    except Exception:
        return 0.0


# ── 1. Corrupt DSL state isolation + alert + metric ──────────────────────

def test_corrupt_dsl_state_is_quarantined_and_alerted(monkeypatch, tmp_path):
    d, state_file = _isolate(monkeypatch, tmp_path)
    state_file.write_text("{ this is not valid json !!!")

    alerts = []
    monkeypatch.setattr(
        "hermes_trader.notify.send_card",
        lambda *a, **k: alerts.append((a, k)))
    before = _counter_value(metrics.DSL_STATE_CORRUPT_ISOLATIONS)

    # Must NOT raise: a corrupt file degrades to an empty registry.
    d.load_state()

    # The corrupt original is moved aside, never left in place / truncated.
    assert not state_file.exists(), "corrupt file must be quarantined"
    quarantined = glob.glob(str(state_file) + ".corrupt-*")
    assert quarantined, "a .corrupt-<ts> sidecar must be created"
    # Risk alert fired with the risk category.
    assert alerts, "a Feishu risk card must be sent on corruption"
    _, kwargs = alerts[0]
    assert kwargs.get("category") == "risk"
    # Metric bumped.
    assert _counter_value(metrics.DSL_STATE_CORRUPT_ISOLATIONS) >= before + 1
    # Registry is safe (empty).
    assert d._active_positions == {}


def test_corrupt_dsl_state_falls_back_to_bak(monkeypatch, tmp_path):
    d, state_file = _isolate(monkeypatch, tmp_path)
    # Seed a healthy state, then force a .bak by rotating (simulate prior save).
    d.register_position("ETH", "long", 100.0)
    d._save_state()
    assert state_file.exists()
    bak = pathlib_bak = str(state_file) + ".bak"
    assert os.path.exists(bak), "a successful save must leave a .bak predecessor"

    # Now corrupt the live file; the .bak holds the healthy tracker.
    d._active_positions.clear()
    d._loaded_from_disk = False
    state_file.write_text("}}}[[]] not json")

    d.load_state()  # must not raise
    assert "ETH_long" in d._active_positions, \
        "loader must recover trackers from the .bak predecessor"
    assert d._active_positions["ETH_long"].entry_px == 100.0


# ── 2. .bak rotation on save ────────────────────────────────────────────

def test_save_state_rotates_bak(monkeypatch, tmp_path):
    d, state_file = _isolate(monkeypatch, tmp_path)
    bak = str(state_file) + ".bak"

    d.register_position("AAA", "long", 10.0)
    d._save_state()
    # First save has no predecessor to rotate; the second save rotates the
    # previous live file into .bak.
    d.register_position("BBB", "long", 20.0)
    d._save_state()
    assert os.path.exists(bak), "second save must retain the prior file as .bak"
    bak_payload = json.loads(open(bak).read())
    coins = {p["coin"] for p in bak_payload.get("positions", [])}
    assert "AAA" in coins, ".bak must hold the previous generation's content"


# ── 3. Rehydrate conservative floor seed from exchange bracket SL ────────

def test_rehydrate_seeds_conservative_floor_from_bracket_sl(monkeypatch, tmp_path):
    d, _ = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        market_regime, "detect_regime",
        lambda coin, *, force=False: "neutral")
    # userFills resolution fails → also exercises the conservative-time path.
    monkeypatch.setattr(d, "_resolve_fill_time_ms", lambda *a, **k: None)

    entry = 100.0
    sl_trigger = 97.0   # backup SL resting 3% below entry (long, adverse side)
    monkeypatch.setattr(
        d, "_fetch_open_orders",
        lambda user: [{"coin": "BTC", "reduceOnly": True, "oid": 555,
                       "limitPx": str(sl_trigger), "sz": "1.0"}])

    d.rehydrate_from_exchange(_positions(("BTC", 1, entry)), user="0xUSER")

    t = d._active_positions["BTC_long"]
    # Bracket was backfilled.
    assert t.sl_oid == 555
    assert t.sl_px == pytest.approx(sl_trigger)
    # Conservative floor seed: the ratchet must start NO LOOSER than the
    # exchange SL (for a long, floor >= sl_trigger), never None.
    assert t._last_floor is not None, "synth tracker must be seeded a floor"
    assert t._last_floor >= sl_trigger - 1e-9, \
        f"long floor {t._last_floor} must not be looser than bracket SL {sl_trigger}"
    # And it must never be seeded ABOVE entry (a profit lock the position never
    # earned would false-exit immediately).
    assert t._last_floor <= entry + 1e-9


def test_rehydrate_short_seeds_conservative_floor(monkeypatch, tmp_path):
    d, _ = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        market_regime, "detect_regime",
        lambda coin, *, force=False: "neutral")
    monkeypatch.setattr(d, "_resolve_fill_time_ms", lambda *a, **k: None)

    entry = 200.0
    sl_trigger = 206.0  # backup SL resting above entry (short, adverse side)
    monkeypatch.setattr(
        d, "_fetch_open_orders",
        lambda user: [{"coin": "ETH", "reduceOnly": True, "oid": 777,
                       "limitPx": str(sl_trigger), "sz": "1.0"}])

    d.rehydrate_from_exchange(_positions(("ETH", -1, entry)), user="0xUSER")

    t = d._active_positions["ETH_short"]
    assert t._last_floor is not None
    # For a short the ratchet moves DOWN; a conservative (tighter) floor is
    # HIGHER, so it must start no looser than the bracket SL (floor <= sl).
    assert t._last_floor <= sl_trigger + 1e-9, \
        f"short floor {t._last_floor} must not be looser than bracket SL {sl_trigger}"
    assert t._last_floor >= entry - 1e-9


# ── 4. Conservative fill-time fallback (not now()) ──────────────────────

def test_fill_time_resolution_failure_ages_conservatively(monkeypatch, tmp_path):
    d, _ = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        market_regime, "detect_regime",
        lambda coin, *, force=False: "neutral")
    monkeypatch.setattr(d, "_resolve_fill_time_ms", lambda *a, **k: None)
    monkeypatch.setattr(d, "_fetch_open_orders", lambda user: [])

    import time as _time
    now = _time.time()
    d.rehydrate_from_exchange(_positions(("SOL", 1, 150.0)), user="0xUSER")

    t = d._active_positions["SOL_long"]
    # The synth tracker must be aged CONSERVATIVELY: its entry_time must be far
    # enough in the past that a normal hard_timeout window has already elapsed
    # (fail-closed), rather than ~now which would reset the zombie clock.
    age_min = (now - t.entry_time) / 60.0
    assert age_min >= t.policy.hard_timeout_minutes - 1.0, (
        f"synth tracker age {age_min:.0f}min must meet/exceed the "
        f"{t.policy.hard_timeout_minutes:.0f}min hard_timeout (conservative)")


# ── 5. _tracker_from_dict defaults ──────────────────────────────────────

def test_tracker_from_dict_stale_flat_defaults_to_480():
    # Persisted policy dict deliberately omits stale_flat_timeout_minutes.
    d = {
        "coin": "DOGE", "side": "long", "entry_px": 0.1,
        "entry_time": 1_700_000_000.0,
        "policy": {"max_loss_pct": 2.0},
    }
    t = dsl_exit._tracker_from_dict(d)
    assert t.policy.stale_flat_timeout_minutes == pytest.approx(480.0), \
        "missing stale_flat_timeout_minutes must hydrate to canonical 480, not 0"


def test_tracker_from_dict_dirty_zero_floor_dropped():
    d = {
        "coin": "DOGE", "side": "long", "entry_px": 0.1,
        "entry_time": 1_700_000_000.0,
        "policy": {},
        "last_floor": 0.0,   # nonsensical for a long (would clamp ratchet to 0)
    }
    t = dsl_exit._tracker_from_dict(d)
    assert t._last_floor is None, "dirty last_floor=0 must be dropped, not trusted"


def test_tracker_from_dict_valid_floor_preserved():
    d = {
        "coin": "DOGE", "side": "long", "entry_px": 0.1,
        "entry_time": 1_700_000_000.0,
        "policy": {},
        "last_floor": 0.105,
    }
    t = dsl_exit._tracker_from_dict(d)
    assert t._last_floor == pytest.approx(0.105)


# ── 6. Executor propagates hard_stop_confirm_sec ────────────────────────

def test_executor_register_propagates_hard_stop_confirm_sec(monkeypatch, tmp_path):
    from hermes_trader.agents import executor
    from hermes_trader.agents import memory as memory_mod

    captured = {}

    def _register(coin, side, px, **kw):
        captured["policy"] = kw.get("policy")

    monkeypatch.setattr(executor, "register_position", _register)
    # Neutralise the side-effecting surfaces _register_filled_position touches.
    # `executor.memory` is the imported AgentMemory singleton.
    monkeypatch.setattr(executor.memory, "record_trade", lambda t: None)
    monkeypatch.setattr(executor.memory, "record_entry_context", lambda *a, **k: None)
    monkeypatch.setattr(
        market_regime, "detect_regime", lambda coin, *, force=False: "neutral")

    config = {"dsl_exit": {"hard_stop_confirm_sec": 2.5}}
    analysis = {"id": "a1", "coin": "BTC"}
    order_res = {"avg_px": 100.0, "total_sz": 1.0, "order_id": "O1"}

    executor._register_filled_position(
        analysis=analysis, config=config, order_res=order_res,
        coin="BTC", trade_side="long", mid_price=100.0, size_in_coin=1.0,
        atr=2.0, leverage=10, user="0xUSER", override_composite=0.0,
        enf=None, aid="a1")

    assert "policy" in captured, "register_position must be called with a policy"
    assert captured["policy"].hard_stop_confirm_sec == pytest.approx(2.5), \
        "hard_stop_confirm_sec from config must reach the ExitPolicy"


# ── 7. Corrupt agent-memory isolation + events rebuild still survives ────

def test_corrupt_memory_is_quarantined_and_rebuilds(monkeypatch, tmp_path):
    import hermes_trader.event_log as event_log
    from hermes_trader.agents import memory as memory_mod

    mem_path = tmp_path / "agent-memory.json"
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_path))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(mem_path) + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(events_path))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(events_path))

    mem_path.write_text("<<< definitely not json >>>")

    alerts = []
    monkeypatch.setattr(
        "hermes_trader.notify.send_card",
        lambda *a, **k: alerts.append((a, k)))
    before = _counter_value(metrics.MEMORY_CORRUPT_ISOLATIONS)

    m = memory_mod.AgentMemory()
    m.load()  # must not raise; events rebuild is the safety net

    assert not mem_path.exists(), "corrupt memory file must be quarantined"
    quarantined = glob.glob(str(mem_path) + ".corrupt-*")
    assert quarantined, "a .corrupt-<ts> sidecar must be created for memory"
    assert alerts, "a Feishu risk card must be sent on memory corruption"
    _, kwargs = alerts[0]
    assert kwargs.get("category") == "risk"
    assert _counter_value(metrics.MEMORY_CORRUPT_ISOLATIONS) >= before + 1


def test_corrupt_memory_falls_back_to_bak(monkeypatch, tmp_path):
    import hermes_trader.event_log as event_log
    from hermes_trader.agents import memory as memory_mod

    mem_path = tmp_path / "agent-memory.json"
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_path))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(mem_path) + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(events_path))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(events_path))

    # Write a healthy memory then rotate it to .bak (predecessor generation).
    m0 = memory_mod.AgentMemory()
    m0._initialized = True
    m0._trades = [{"id": "t1", "coin": "BTC"}]
    m0._dirty = True
    m0.flush(force=True)
    bak = str(mem_path) + ".bak"
    assert os.path.exists(bak), "a successful memory flush must leave a .bak"

    # Corrupt the live file; the .bak still holds the BTC trade.
    mem_path.write_text("not-json-at-all")
    m1 = memory_mod.AgentMemory()
    m1.load()  # must not raise
    coins = {t.get("coin") for t in m1._trades}
    assert "BTC" in coins, "memory must hydrate from the .bak predecessor"
