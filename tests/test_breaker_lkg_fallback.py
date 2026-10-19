"""P1-4 (2026-10-19): breaker last-known-good (LKG) verdict fallback.

The memory-backed breakers used to fail *blindly open* on a state-read error.
They now remember the last verdict each gate computed from a GOOD read and,
while memory is unreadable, keep enforcing that verdict:

  * last verdict BLOCK (breaker armed) → keep blocking (fail-safe);
  * last verdict PASS  (breaker clear) → keep passing (no phantom block);
  * no fresh LKG (cold start / TTL expiry) → the historical alerted fail-open.

The loudness triplet (error log + Prometheus counter + Feishu/SSE blind alert)
still fires in every read-failure branch; LKG only changes the returned verdict.
"""

import time

import pytest


def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="DOGE",
                trade_side="long", has_binary_news_risk=False, equity=1000.0,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def _isolated_memory(monkeypatch, tmp_path):
    import hermes_trader.event_log as event_log
    from hermes_trader.agents import memory as memory_mod
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


def _boom(*_a, **_k):
    raise RuntimeError("simulated memory read failure")


@pytest.fixture(autouse=True)
def _isolate_lkg():
    from hermes_trader.agents import risk_gates
    risk_gates.reset_breaker_lkg()
    yield
    risk_gates.reset_breaker_lkg()


# ── coin circuit ──────────────────────────────────────────────────────────

def test_coin_circuit_cold_failure_fails_open(monkeypatch, tmp_path):
    """No prior verdict → read failure degrades to the alerted fail-open."""
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import coin_circuit_breaker_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min", _boom)
    r = coin_circuit_breaker_gate(_ctx())
    assert r["pass"] is True
    assert "lkg" not in r  # plain fail-open, not an LKG replay


def test_coin_circuit_holds_last_armed_block_when_memory_blinded(monkeypatch, tmp_path):
    """armed → then memory read raises → the gate keeps blocking (fail-safe)."""
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import coin_circuit_breaker_gate
    _isolated_memory(monkeypatch, tmp_path)
    future = int(time.time() * 1000) + 30 * 60_000
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min",
                        lambda coin: 30.0 if coin == "DOGE" else 0.0)
    armed = coin_circuit_breaker_gate(_ctx())
    assert armed["pass"] is False
    # Now the state read blows up.
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min", _boom)
    held = coin_circuit_breaker_gate(_ctx())
    assert held["pass"] is False
    assert held.get("via") == "lkg_fallback"
    assert "LKG" in held["reason"]


def test_clear_verdict_overwrites_armed_then_passes_through_outage(monkeypatch, tmp_path):
    """A later GOOD clear tick must replace a cached armed verdict, so the LKG
    can never wedge a coin closed after the breaker genuinely cleared."""
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import coin_circuit_breaker_gate
    _isolated_memory(monkeypatch, tmp_path)
    # armed tick
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min", lambda coin: 30.0)
    assert coin_circuit_breaker_gate(_ctx())["pass"] is False
    # good clear tick (breaker expired)
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min", lambda coin: 0.0)
    assert coin_circuit_breaker_gate(_ctx())["pass"] is True
    # outage now → replays the CLEAR verdict (pass), no phantom block
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min", _boom)
    r = coin_circuit_breaker_gate(_ctx())
    assert r["pass"] is True
    assert r.get("via") == "lkg_fallback_clear"


def test_lkg_is_scoped_per_coin(monkeypatch, tmp_path):
    """An armed LKG on DOGE must not block an unrelated coin on read failure."""
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import coin_circuit_breaker_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min",
                        lambda coin: 30.0 if coin == "DOGE" else 0.0)
    assert coin_circuit_breaker_gate(_ctx(coin="DOGE"))["pass"] is False
    # Different coin has never been evaluated armed; with no LKG it fail-opens.
    monkeypatch.setattr(memory_mod.memory, "coin_circuit_remaining_min", _boom)
    assert coin_circuit_breaker_gate(_ctx(coin="ETH"))["pass"] is True


# ── global halt (book-wide key) ───────────────────────────────────────────

def test_global_halt_holds_armed_block_then_clears(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import global_halt_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "global_halt_remaining_min", lambda: 120.0)
    assert global_halt_gate(_ctx())["pass"] is False
    monkeypatch.setattr(memory_mod.memory, "global_halt_remaining_min", _boom)
    held = global_halt_gate(_ctx())
    assert held["pass"] is False and held.get("via") == "lkg_fallback"
    # recovery (good read says clear) then outage → pass
    monkeypatch.setattr(memory_mod.memory, "global_halt_remaining_min", lambda: 0.0)
    assert global_halt_gate(_ctx())["pass"] is True
    monkeypatch.setattr(memory_mod.memory, "global_halt_remaining_min", _boom)
    assert global_halt_gate(_ctx())["pass"] is True


# ── consecutive loss ──────────────────────────────────────────────────────

def test_consecutive_loss_holds_armed_block(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "consecutive_losses", lambda coin: 4)
    assert consecutive_loss_gate(_ctx(), limit=3)["pass"] is False
    monkeypatch.setattr(memory_mod.memory, "consecutive_losses", _boom)
    held = consecutive_loss_gate(_ctx(), limit=3)
    assert held["pass"] is False and held.get("via") == "lkg_fallback"


def test_consecutive_loss_disabled_gate_is_not_cached(monkeypatch, tmp_path):
    """limit<=0 short-circuits before any read; a later enabled-but-blinded
    evaluation with no real LKG must fail open, not invent a block."""
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    _isolated_memory(monkeypatch, tmp_path)
    assert consecutive_loss_gate(_ctx(), limit=0)["pass"] is True
    monkeypatch.setattr(memory_mod.memory, "consecutive_losses", _boom)
    assert consecutive_loss_gate(_ctx(), limit=3)["pass"] is True


# ── per-coin daily loss ───────────────────────────────────────────────────

def test_per_coin_daily_loss_no_baseline_pass_is_not_cached_as_clear(monkeypatch, tmp_path):
    """The pre-first-tick sod<=0 pass is not a meaningful verdict: a subsequent
    blinded read with no real LKG must still fail open (no cached CLEAR)."""
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "get_start_of_day_equity", lambda: 0.0)
    assert per_coin_daily_loss_gate(_ctx(), max_loss_pct=3.0)["pass"] is True
    monkeypatch.setattr(memory_mod.memory, "get_start_of_day_equity", _boom)
    r = per_coin_daily_loss_gate(_ctx(), max_loss_pct=3.0)
    assert r["pass"] is True and "via" not in r


def test_per_coin_daily_loss_holds_armed_block(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "get_start_of_day_equity", lambda: 1000.0)
    monkeypatch.setattr(memory_mod.memory, "coin_daily_realized_pnl_pct",
                        lambda coin, sod: -5.0)
    assert per_coin_daily_loss_gate(_ctx(), max_loss_pct=3.0)["pass"] is False
    monkeypatch.setattr(memory_mod.memory, "coin_daily_realized_pnl_pct", _boom)
    held = per_coin_daily_loss_gate(_ctx(), max_loss_pct=3.0)
    assert held["pass"] is False and held.get("via") == "lkg_fallback"


# ── drawdown ──────────────────────────────────────────────────────────────

def test_drawdown_holds_armed_block_when_blinded(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod, risk_gates
    from hermes_trader.agents.risk_gates import drawdown_gate
    m = _isolated_memory(monkeypatch, tmp_path)
    # Seed a rolling trail directly (same-second track_daily_pnl ticks collapse
    # under the 600s min-spacing rule): an in-window $1000 peak and a current
    # $800 level = -20%, beyond the 15% cap.
    now_s = time.time()
    m._peak_equity = 1000.0
    m._equity_trail.clear()
    m._equity_trail.append((now_s - 3600.0, 1000.0, 0.0))
    m._equity_trail.append((now_s, 800.0, 0.0))
    real_cfg_get = risk_gates.cfg_get
    forced = {
        "circuit_breaker.drawdown_peak_window_days": 14.0,
        "circuit_breaker.drawdown_cooldown_hours": 24.0,
    }
    monkeypatch.setattr(risk_gates, "cfg_get",
                        lambda k, d=None, *, config=None: forced.get(k, real_cfg_get(k, d, config=config)))
    armed = drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)
    assert armed["pass"] is False
    # Blind every memory accessor the gate touches (peak + trail + freeze).
    monkeypatch.setattr(memory_mod.memory, "peak_equity", _boom)
    monkeypatch.setattr(memory_mod.memory, "rolling_peak_equity", _boom)
    monkeypatch.setattr(memory_mod.memory, "mark_drawdown_frozen", _boom)
    monkeypatch.setattr(memory_mod.memory, "_equity_trail", property(lambda self: (_ for _ in ()).throw(RuntimeError("boom"))))
    held = drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)
    assert held["pass"] is False and held.get("via") == "lkg_fallback"


def test_drawdown_no_baseline_pass_not_cached(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import drawdown_gate
    _isolated_memory(monkeypatch, tmp_path)
    # equity<=0 is a no-baseline pass; not cached, so the later blind read
    # (positive equity but unreadable memory) cold-fails-open.
    assert drawdown_gate(_ctx(equity=0.0), max_drawdown_pct=15.0)["pass"] is True
    monkeypatch.setattr(memory_mod.memory, "peak_equity", _boom)
    r = drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)
    assert r["pass"] is True and "via" not in r


# ── TTL expiry ────────────────────────────────────────────────────────────

def test_expired_lkg_degrades_to_fail_open(monkeypatch, tmp_path):
    """An outage longer than the LKG TTL must not wedge the book closed on a
    stale armed verdict forever — it falls back to the alerted fail-open."""
    from hermes_trader.agents import memory as memory_mod, risk_gates
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(risk_gates, "_BREAKER_LKG_TTL_S", 0.0)  # always stale
    future_key = risk_gates._lkg_key("global_halt", _ctx())
    # Seed an armed verdict directly, dated "now".
    risk_gates._breaker_record_lkg("global_halt", _ctx(),
                                   {"pass": False, "reason": "global daily-loss halt active"})
    # With TTL=0 the entry is immediately expired → no replay.
    assert risk_gates._breaker_lkg_result("global_halt", _ctx()) is None
    assert future_key in risk_gates._breaker_lkg  # entry still present, just stale
