"""Deep-audit (2026-08-28) remediation tests: B-M4, B-M12, C-M1.

B-M4:  opposite_direction_guard must fail CLOSED on a malformed held-position
       record (missing 'side') instead of passing the trade through.
B-M12: risk-blocking memory state (per-coin circuit, global halt, loss
       cooldown) must force-flush to disk immediately — a crash inside the
       flush-throttle window must not lose the block.
C-M1:  a held coin with no usable mark price is a blind DSL tick: it must be
       logged loudly (not silently skipped), reported by the feed-health
       helper, and stop new-entry decisions for the cycle.
"""
import json
import logging

# ── B-M4: opposite_direction_guard fail-closed on malformed position ──────

def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="BTC",
                trade_side="long", has_binary_news_risk=False, equity=1000,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def test_bm4_malformed_held_position_fails_closed():
    """A held record missing 'side' is corrupted state: both flip and pyramid
    are dangerous, so the guard must block (pass=False) with a clear reason."""
    from hermes_trader.agents.risk_gates import opposite_direction_guard
    malformed = [{"coin": "ETH"}]  # held position with no 'side' key
    # blocking in BOTH intended directions
    r_long = opposite_direction_guard(
        _ctx(coin="ETH", trade_side="long", current_positions=malformed))
    assert r_long["pass"] is False
    assert "malformed_position" in r_long["reason"]
    r_short = opposite_direction_guard(
        _ctx(coin="ETH", trade_side="short", current_positions=malformed))
    assert r_short["pass"] is False
    assert "malformed_position" in r_short["reason"]


def test_bm4_malformed_record_logs_at_error(caplog):
    from hermes_trader.agents.risk_gates import opposite_direction_guard
    with caplog.at_level(logging.ERROR, logger="hermes_trader.agents.risk_gates"):
        opposite_direction_guard(
            _ctx(coin="ETH", trade_side="long",
                 current_positions=[{"coin": "ETH"}]))
    assert any("malformed" in rec.getMessage().lower()
               for rec in caplog.records)


def test_bm4_well_formed_positions_still_guard_normally():
    """Regression: the fail-closed branch must not change the three normal
    outcomes (same-side pyramid block, opposite flip block, unheld pass)."""
    from hermes_trader.agents.risk_gates import opposite_direction_guard
    held = [{"coin": "ETH", "side": "long", "size_usd": 100}]
    assert opposite_direction_guard(
        _ctx(coin="ETH", trade_side="long", current_positions=held))["pass"] is False
    assert opposite_direction_guard(
        _ctx(coin="ETH", trade_side="short", current_positions=held))["pass"] is False
    assert opposite_direction_guard(
        _ctx(coin="SOL", trade_side="long", current_positions=held))["pass"] is True


# ── B-M12: risk-blocking setters force-flush immediately ──────────────────

def _isolated_memory(monkeypatch, tmp_path):
    """AgentMemory pointed at tmp paths (memory + lock + events), hydrated."""
    from hermes_trader.agents import memory as memory_mod
    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    m = memory_mod.AgentMemory()
    m.load()
    return m, mem_path


def _read_persisted(path):
    with open(path, "r") as f:
        return json.load(f)


def test_bm12_coin_circuit_force_flushes_within_throttle_window(monkeypatch, tmp_path):
    """set_coin_circuit must land on disk even when called twice back-to-back
    (the second call is inside the flush-throttle window)."""
    m, path = _isolated_memory(monkeypatch, tmp_path)
    until = 9_000_000_000_000  # far future ms
    m.set_coin_circuit("ETH", until)
    m.set_coin_circuit("SOL", until)  # throttled under the old (buggy) behaviour
    data = _read_persisted(path)
    assert data["coinCircuit"].get("ETH") == until
    assert data["coinCircuit"].get("SOL") == until


def test_bm12_global_halt_force_flushes_within_throttle_window(monkeypatch, tmp_path):
    m, path = _isolated_memory(monkeypatch, tmp_path)
    until = 9_000_000_000_000
    m.set_global_halt(until)
    data = _read_persisted(path)
    assert data["globalHaltUntilMs"] == until


def test_bm12_loss_cooldown_force_flushes_within_throttle_window(monkeypatch, tmp_path):
    """Anti-revenge cooldown is the same class of risk-blocking state."""
    m, path = _isolated_memory(monkeypatch, tmp_path)
    until = 9_000_000_000_000
    m.set_loss_cooldown("ETH", until)
    m.set_loss_cooldown("SOL", until)
    data = _read_persisted(path)
    cd = {c["coin"]: c["expires"] for c in data["cooldowns"]}
    assert cd.get("ETH") == until
    assert cd.get("SOL") == until


def test_bm12_breaker_state_survives_simulated_restart(monkeypatch, tmp_path):
    """End-to-end durability: breaker armed → new AgentMemory rehydrates it."""
    from hermes_trader.agents import memory as memory_mod
    m, path = _isolated_memory(monkeypatch, tmp_path)
    until = 9_000_000_000_000
    m.set_coin_circuit("ETH", until)
    m.set_global_halt(until)

    m2 = memory_mod.AgentMemory()
    m2.load()
    assert m2.coin_circuit_remaining_min("ETH") > 0
    assert m2.global_halt_remaining_min() > 0


def test_bm12_setters_call_flush_with_force_true(monkeypatch, tmp_path):
    """Source-level guard: all three risk-blocking setters must pass
    force=True (a plain self.flush() would be throttled)."""
    import inspect

    from hermes_trader.agents import memory as memory_mod
    src = inspect.getsource(memory_mod.AgentMemory)
    for setter in ("set_coin_circuit", "set_global_halt", "set_loss_cooldown"):
        body = inspect.getsource(getattr(memory_mod.AgentMemory, setter))
        assert "flush(force=True)" in body, f"{setter} missing force flush"
    # sanity: the method bodies exist in the class source
    assert "set_coin_circuit" in src


# ── C-M1: blind DSL ticks are loud + feed-health gate ─────────────────────

def _isolate_dsl(monkeypatch, tmp_path):
    from hermes_trader.agents import dsl_exit
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(tmp_path / "dsl.json"))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit._last_missing_mid_warn.clear()
    return dsl_exit


def test_cm1_missing_mid_logs_error_instead_of_silent_skip(monkeypatch, tmp_path, caplog):
    """Held coin absent from mids: no exit verdict (no price → no defensible
    close), but an ERROR log is emitted — previously a silent `continue`."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 100.0)
    with caplog.at_level(logging.ERROR, logger="hermes_trader.agents.dsl_exit"):
        verdicts = dsl_exit.check_all_positions({})  # feed dead
    assert verdicts == []
    assert any("NO USABLE MID" in rec.getMessage() and "ETH" in rec.getMessage()
               for rec in caplog.records)


def test_cm1_garbage_mid_values_are_blind_ticks(monkeypatch, tmp_path, caplog):
    """Non-numeric / zero / negative / NaN mids are all unusable."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 100.0)
    dsl_exit.register_position("SOL", "short", 100.0)
    dsl_exit.register_position("BTC", "long", 50000.0)
    with caplog.at_level(logging.ERROR, logger="hermes_trader.agents.dsl_exit"):
        verdicts = dsl_exit.check_all_positions(
            {"ETH": "not-a-number", "SOL": 0.0, "BTC": -1.0, "XRP": float("nan")})
    assert verdicts == []
    # message format: "[dsl] NO USABLE MID for held {coin} {side} ..."
    blinded = {rec.getMessage().split()[6] for rec in caplog.records
               if "NO USABLE MID" in rec.getMessage()}
    assert "ETH" in blinded and "SOL" in blinded and "BTC" in blinded


def test_cm1_healthy_mids_still_evaluate_exits(monkeypatch, tmp_path, caplog):
    """Regression: valid prices evaluate normally and fire the max-loss exit."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    # H-5 (supplemental audit 2026-08-30): the hard stop now wick-guards — the
    # breach must persist hard_stop_confirm_sec (default 1.0s) AND be confirmed
    # by the index (oracle). This is one synchronous tick in a synthetic 100.0
    # world; pin the window to 0 and feed a consistent test index (stubbed, no
    # network) so the immediate max-loss verdict this test asserts can fire.
    policy = dsl_exit.ExitPolicy(hard_stop_confirm_sec=0.0)
    dsl_exit.register_position("ETH", "long", 100.0, policy=policy)
    monkeypatch.setattr(dsl_exit, "get_index_prices",
                        lambda coins: {"ETH": 96.0})
    with caplog.at_level(logging.ERROR, logger="hermes_trader.agents.dsl_exit"):
        verdicts = dsl_exit.check_all_positions({"ETH": 96.0})  # -4% > 2.5% cap
    assert len(verdicts) == 1 and verdicts[0].exit is True
    assert not any("NO USABLE MID" in rec.getMessage() for rec in caplog.records)


def test_cm1_missing_mid_warn_is_throttled_per_coin(monkeypatch, tmp_path, caplog):
    """The alarm must not spam every tick: one ERROR per coin per interval."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 100.0)
    with caplog.at_level(logging.ERROR, logger="hermes_trader.agents.dsl_exit"):
        for _ in range(5):
            dsl_exit.check_all_positions({})
    alarms = [rec for rec in caplog.records if "NO USABLE MID" in rec.getMessage()]
    assert len(alarms) == 1


def test_cm1_held_coins_missing_mids_helper(monkeypatch, tmp_path):
    """Feed-health helper used by the trading loop entry gate."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 100.0)
    dsl_exit.register_position("SOL", "short", 100.0)
    # empty snapshot → every held coin is blind
    assert dsl_exit.held_coins_missing_mids({}) == ["ETH", "SOL"]
    # partial feed → only the missing coin reported
    assert dsl_exit.held_coins_missing_mids({"ETH": 101.0}) == ["SOL"]
    # full feed → healthy
    assert dsl_exit.held_coins_missing_mids({"ETH": 101.0, "SOL": 99.0}) == []
    # garbage value counts as missing
    assert dsl_exit.held_coins_missing_mids({"ETH": "x", "SOL": 99.0}) == ["ETH"]


def test_cm1_no_positions_means_healthy_feed(monkeypatch, tmp_path):
    """Flat book: an empty snapshot is NOT a feed halt for entries (nothing
    held to be blind to) — the loop treats empty mids separately."""
    dsl_exit = _isolate_dsl(monkeypatch, tmp_path)
    assert dsl_exit.held_coins_missing_mids({}) == []
    assert dsl_exit.held_coins_missing_mids({"ETH": 100.0}) == []
