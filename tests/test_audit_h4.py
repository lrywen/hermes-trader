"""Deep-audit (2026-08-29) remediation tests: H4 — pre-trade liquidation-price
pre-check gate (``liquidation_buffer``).

The audit's 10U thin-margin blast scenario: on a $10 equity account a single
order at 10x with a ~3% backup stop is STRUCTURALLY guaranteed to be
liquidated before the stop can fire — isolated-margin liq sits ~9% from entry
(100/lev - maintenance margin), i.e. well outside the 3% stop bracket, but
that means a 3% adverse move loses ~30% of the 10U margin while the actual
liq trigger at 9% wipes it entirely; the gate enforces the audit contract::

    liq_distance_pct (= 100/leverage - maint_margin_pct)
        > stop_distance_pct + sl_buffer_bps/100

The isolated-margin estimate is deliberately CONSERVATIVE for cross margin
(real cross liq is further away — account-wide collateral backs the
position), so the gate only refuses orders fatal even in isolation.

Failure semantics:
  * maint_margin_rate_pct <= 0            → gate DISABLED (pass open)
  * entry_px / leverage / stop_distance
    any zero (data not supplied)          → pass open (manual-order path)
  * liq_distance_pct <= 0 (absurd lev)    → fail CLOSED (no cushion at all)
  * liq_distance <= stop + buffer         → fail CLOSED
"""

# ── helpers ───────────────────────────────────────────────────────────────

def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="ETH",
                trade_side="long", has_binary_news_risk=False, equity=1000.0,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def _isolated_memory(monkeypatch, tmp_path):
    """AgentMemory pointed at tmp paths and installed as the module singleton
    (eval_all_gates reads memory via the gates module)."""
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


# ── 1. liquidation_buffer_gate: unit ──────────────────────────────────────

def test_h4_blocks_high_leverage_inside_stop_bracket():
    """20x with a 4.5% worst-case stop: liq = 100/20 - 1 = 4.0% away, while
    the bracket requires 4.5% + 0.1% buffer = 4.6% → liq sits INSIDE the
    stop bracket → BLOCKED."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    ctx = _ctx(entry_px=3000.0, leverage=20.0, stop_distance_pct=4.5)
    r = liquidation_buffer_gate(ctx, maint_margin_rate_pct=1.0,
                                extra_buffer_pct=0.1)
    assert r["pass"] is False
    reason = r["reason"]
    assert "liquidation_buffer" in reason and "ETH" in reason and "20x" in reason


def test_h4_blocks_the_canonical_10u_blast_case():
    """The audit's headline: 10x leverage with a stop bracket wide enough
    (slippage-widened on a thin book) to reach the ~9% liq distance.
    stop 8.5% + 0.6% buffer = 9.1% > 9.0% liq → BLOCKED."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    ctx = _ctx(coin="HYPE", trade_side="long",
               entry_px=20.0, leverage=10.0, stop_distance_pct=8.5)
    r = liquidation_buffer_gate(ctx, maint_margin_rate_pct=1.0,
                                extra_buffer_pct=0.6)
    assert r["pass"] is False
    assert "HYPE" in r["reason"]


def test_h4_passes_safe_low_leverage():
    """3x + 1.5% stop: liq = 33.3 - 1 = 32.3% ≫ 1.6% required → PASS."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    ctx = _ctx(entry_px=3000.0, leverage=3.0, stop_distance_pct=1.5)
    r = liquidation_buffer_gate(ctx, maint_margin_rate_pct=1.0,
                                extra_buffer_pct=0.1)
    assert r["pass"] is True


def test_h4_passes_when_liq_just_outside_bracket():
    """Boundary: liq 9.0% vs required exactly 9.0% → the contract is STRICT
    greater-than, so equality BLOCKS (liq must clear the bracket, not touch
    it)."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    # 10x, 1% maint → liq = 9.0%; stop 8.9 + 0.1 buffer = 9.0 → blocked
    ctx_eq = _ctx(entry_px=100.0, leverage=10.0, stop_distance_pct=8.9)
    assert liquidation_buffer_gate(ctx_eq, 1.0, 0.1)["pass"] is False
    # stop 8.8 + 0.1 = 8.9 < 9.0 → passes
    ctx_ok = _ctx(entry_px=100.0, leverage=10.0, stop_distance_pct=8.8)
    assert liquidation_buffer_gate(ctx_ok, 1.0, 0.1)["pass"] is True


def test_h4_disabled_when_maint_rate_zero():
    """maint_margin_rate_pct <= 0 disables the gate entirely (operator
    opt-out)."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    ctx = _ctx(entry_px=100.0, leverage=50.0, stop_distance_pct=10.0)
    assert liquidation_buffer_gate(ctx, 0.0, 0.1)["pass"] is True
    assert liquidation_buffer_gate(ctx, -1.0, 0.1)["pass"] is True


def test_h4_no_data_passes_open():
    """Zero fields (manual-order GateContext never supplies H4 inputs) →
    nothing to check → pass open."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    assert liquidation_buffer_gate(_ctx(), 1.0, 0.1)["pass"] is True
    # each field individually zero also passes open
    assert liquidation_buffer_gate(
        _ctx(leverage=10.0, stop_distance_pct=3.0), 1.0, 0.1)["pass"] is True
    assert liquidation_buffer_gate(
        _ctx(entry_px=100.0, stop_distance_pct=3.0), 1.0, 0.1)["pass"] is True
    assert liquidation_buffer_gate(
        _ctx(entry_px=100.0, leverage=10.0), 1.0, 0.1)["pass"] is True


def test_h4_absurd_leverage_no_cushion_fails_closed():
    """liq_distance_pct <= 0 (leverage so high maintenance margin alone
    exceeds the 1/lev move) → fail CLOSED with a distinct reason."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    # 200x → 100/200 = 0.5% gross move; maint 1% → liq_distance = -0.5%
    ctx = _ctx(entry_px=100.0, leverage=200.0, stop_distance_pct=0.2)
    r = liquidation_buffer_gate(ctx, maint_margin_rate_pct=1.0,
                                extra_buffer_pct=0.1)
    assert r["pass"] is False
    assert "no liq cushion" in r["reason"] or "liq cushion" in r["reason"]


def test_h4_extra_buffer_uses_sl_buffer_bps_contract():
    """A bigger extra buffer (sl_buffer_bps) tightens the gate: a setup that
    passes with 0 buffer blocks with a wide one."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    # 10x → liq 9.0%; stop 8.95%
    ctx = _ctx(entry_px=100.0, leverage=10.0, stop_distance_pct=8.95)
    assert liquidation_buffer_gate(ctx, 1.0, 0.0)["pass"] is True   # req 8.95
    assert liquidation_buffer_gate(ctx, 1.0, 0.1)["pass"] is False  # req 9.05


def test_h4_short_side_treated_symmetrically():
    """The liq-distance estimate is side-agnostic (a short is liquidated on
    the same % move up)."""
    from hermes_trader.agents.risk_gates import liquidation_buffer_gate
    ctx = _ctx(trade_side="short", entry_px=100.0, leverage=20.0,
               stop_distance_pct=4.5)
    r = liquidation_buffer_gate(ctx, 1.0, 0.1)
    assert r["pass"] is False
    assert "short" in r["reason"]


# ── 2. GateContext plumbing ───────────────────────────────────────────────

def test_h4_gatecontext_fields_default_to_zero():
    """Existing call sites (manual path, 9 test constructors) build
    GateContext without H4 fields — they must default to 0.0 and coerce."""
    ctx = _ctx()
    assert ctx.entry_px == 0.0 and ctx.leverage == 0.0
    assert ctx.stop_distance_pct == 0.0


def test_h4_gatecontext_numeric_coercion():
    """__post_init__ coerces H4 inputs; garbage fails safe to 0.0 (gate then
    passes open rather than crashing the whole chain)."""
    ctx = _ctx(entry_px="3000", leverage="10", stop_distance_pct="3.5")
    assert ctx.entry_px == 3000.0 and ctx.leverage == 10.0
    assert ctx.stop_distance_pct == 3.5
    ctx_bad = _ctx(entry_px=None, leverage="oops", stop_distance_pct=None)
    assert ctx_bad.entry_px == 0.0 and ctx_bad.leverage == 0.0
    assert ctx_bad.stop_distance_pct == 0.0


# ── 3. eval_all_gates wiring ──────────────────────────────────────────────

# Permissive config so ONLY liquidation_buffer can block the proposal.
_WIRING_CONFIG = {
    "debate_gate": {"enabled": False},
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
    "news_blackout": {"enabled": False},
    "circuit_breaker": {
        "consecutive_loss_limit": 0,
        "coin_daily_loss_pct": 0.0,
        "max_drawdown_pct": 0.0,
    },
    # H4 knobs:
    "liquidation_maint_margin_pct": 1.0,
    "sl_buffer_bps": 10.0,  # → 0.1% extra buffer
}


def test_h4_wiring_key_present_and_passes_when_clean(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import eval_all_gates
    _isolated_memory(monkeypatch, tmp_path)
    report = eval_all_gates(
        _ctx(coin="ETH", equity=1000.0, entry_px=3000.0, leverage=3.0,
             stop_distance_pct=1.5),
        dict(_WIRING_CONFIG))
    assert "liquidation_buffer" in report["results"]
    assert report["results"]["liquidation_buffer"]["pass"] is True
    assert report["blocked"] is False, report["block_reasons"]


def test_h4_wiring_blocks_through_eval(monkeypatch, tmp_path):
    """A fatal leverage/stop combination blocks the WHOLE proposal via
    eval_all_gates and surfaces the reason."""
    from hermes_trader.agents.risk_gates import eval_all_gates
    _isolated_memory(monkeypatch, tmp_path)
    report = eval_all_gates(
        _ctx(coin="ETH", equity=10.0, entry_px=3000.0, leverage=20.0,
             stop_distance_pct=4.5),
        dict(_WIRING_CONFIG))
    assert report["results"]["liquidation_buffer"]["pass"] is False
    assert report["blocked"] is True
    assert any("liquidation_buffer" in r for r in report["block_reasons"])


def test_h4_wiring_manual_path_zero_fields_pass_open(monkeypatch, tmp_path):
    """The manual-order GateContext (no H4 fields) must NOT be blocked by H4
    even on a 10U account — manual orders are operator-vetted."""
    from hermes_trader.agents.risk_gates import eval_all_gates
    _isolated_memory(monkeypatch, tmp_path)
    report = eval_all_gates(
        _ctx(coin="ETH", confidence=1.0, equity=10.0),  # no entry/lev/stop
        dict(_WIRING_CONFIG))
    assert report["results"]["liquidation_buffer"]["pass"] is True


def test_h4_wiring_zero_maint_rate_disables(monkeypatch, tmp_path):
    """liquidation_maint_margin_pct=0 disables the gate in the chain."""
    from hermes_trader.agents.risk_gates import eval_all_gates
    _isolated_memory(monkeypatch, tmp_path)
    cfg = dict(_WIRING_CONFIG)
    cfg["liquidation_maint_margin_pct"] = 0.0
    report = eval_all_gates(
        _ctx(coin="ETH", equity=10.0, entry_px=3000.0, leverage=200.0,
             stop_distance_pct=10.0),
        cfg)
    assert report["results"]["liquidation_buffer"]["pass"] is True


def test_h4_wiring_sl_buffer_bps_feeds_extra_buffer(monkeypatch, tmp_path):
    """sl_buffer_bps is converted basis-points → percent (10 bps = 0.1%)."""
    from hermes_trader.agents.risk_gates import eval_all_gates
    _isolated_memory(monkeypatch, tmp_path)
    # 10x → liq 9.0%; stop 8.95%: passes with 0 buffer, blocks at 0.1%.
    ctx = _ctx(coin="ETH", equity=1000.0, entry_px=100.0, leverage=10.0,
               stop_distance_pct=8.95)
    cfg_no_buf = dict(_WIRING_CONFIG); cfg_no_buf["sl_buffer_bps"] = 0.0
    r0 = eval_all_gates(ctx, cfg_no_buf)
    assert r0["results"]["liquidation_buffer"]["pass"] is True
    r1 = eval_all_gates(ctx, dict(_WIRING_CONFIG))  # 10 bps → 0.1%
    assert r1["results"]["liquidation_buffer"]["pass"] is False


# ── 4. config registration ────────────────────────────────────────────────

def test_h4_canonical_default_registered():
    """liquidation_maint_margin_pct ships enabled with a conservative 1%
    flat assumption (HL's real tier-dependent rate for small perps)."""
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get
    assert "liquidation_maint_margin_pct" in CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["liquidation_maint_margin_pct"] == 1.0
    # cfg_get falls back to the canonical default on an empty config.
    assert float(cfg_get("liquidation_maint_margin_pct", config={})) == 1.0


def test_h4_schema_field_bounds():
    """The pydantic schema accepts 0 (disabled) and sane rates, rejecting
    negatives and implausible values."""
    from hermes_trader.agents.config_schema import _ConfigPatch
    assert _ConfigPatch(liquidation_maint_margin_pct=0.0).liquidation_maint_margin_pct == 0.0
    assert _ConfigPatch(liquidation_maint_margin_pct=1.5).liquidation_maint_margin_pct == 1.5
    import pydantic
    for bad in (-0.1, 20.1, 999.0):
        try:
            _ConfigPatch(liquidation_maint_margin_pct=bad)
        except pydantic.ValidationError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError(f"schema accepted out-of-range rate {bad}")


# ── 5. executor wiring: stop-distance estimate feeds the gate ─────────────

def test_h4_executor_passes_leverage_and_stop_estimate(monkeypatch, tmp_path):
    """maybe_execute builds the GateContext with leverage from config and a
    stop-distance estimate derived from ATR (mirroring the backup-SL clamp).
    We capture the GateContext handed to eval_all_gates and assert the H4
    fields are populated; a fatal combo must make the proposal return
    blocked without any order being placed."""
    from hermes_trader.agents import executor, risk_gates, memory as memory_mod
    import hermes_trader.event_log as event_log

    # Isolated memory.
    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    events_path = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", events_path)
    monkeypatch.setattr(event_log, "EVENTS_FILE", events_path)
    mem = memory_mod.AgentMemory(); mem.load()
    monkeypatch.setattr(memory_mod, "memory", mem)

    # Config: 20x leverage, ATR sizing OFF (legacy path — exercises the
    # mid_price prefetch), tiny equity so nothing else interferes.
    cfg = {
        "mode": "LIVE", "enable_crypto": True,
        "leverage": 20,
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
        "counter_regime_min_conf": 0.0,
        "block_counter_trend_bypass": False,
        "crowded_with_min_conf": 0.0,
        "debate_gate": {"enabled": False},
        "news_blackout": {"enabled": False},
        "circuit_breaker": {"consecutive_loss_limit": 0,
                            "coin_daily_loss_pct": 0.0,
                            "max_drawdown_pct": 0.0},
        "liquidation_maint_margin_pct": 1.0,
        "sl_buffer_bps": 10.0,
        # Backup-SL clamp constants: floor 1.2% / ceiling 3.0% / mult 1.5.
        "sl_atr_mult": 1.5, "sl_floor_pct": 1.2, "sl_ceiling_pct": 3.0,
    }
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(cfg))
    monkeypatch.setattr(executor, "get_max_leverage", lambda _c: 50)
    # Account state: healthy $1000 book, no open positions, ample free margin.
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": 1000.0, "available": 900.0,
                                           "total_ntl": 0.0, "asset_positions": []})

    # Market data: mid 100; ATR huge (10 → atr/mid*1.5*100 = 15% → clamped to
    # ceiling 3%; worst-case stop = 3 + 1.5 = 4.5%). 20x → liq 4.0% < 4.6%
    # required → BLOCKED.
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 10.0)
    # Order-layer normalization (legacy sizing path normalizes size pre-gate).
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)

    captured = {}
    real_eval = risk_gates.eval_all_gates

    def _spy_eval(ctx, config, *args, **kw):
        captured["ctx"] = ctx
        return real_eval(ctx, config, *args, **kw)

    monkeypatch.setattr(executor, "eval_all_gates", _spy_eval)

    # No order must ever be placed when the gate blocks.
    def _no_place(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("order placement reached despite H4 block")
    monkeypatch.setattr(executor, "place_hl_order", _no_place)

    analysis = {
        "id": "h4test", "coin": "ETH", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 95.0, "tp_px": 110.0,
        "reasoning": "h4 test",
    }
    result = executor.maybe_execute(analysis)

    # The H4 inputs reached the gate...
    ctx = captured["ctx"]
    assert ctx.leverage == 20.0
    assert ctx.entry_px == 100.0
    # width clamps to ceiling 3.0; worst-case = min(3+1.5, 4.5) = 4.5
    assert abs(ctx.stop_distance_pct - 4.5) < 1e-9
    # ...and the proposal was blocked (not executed).
    assert result.get("executed") is False
    assert "blocked_by" in result
    assert any("liquidation_buffer" in r for r in result["blocked_by"])


def test_h4_executor_estimate_failure_leaves_zero_pass_open(monkeypatch, tmp_path):
    """If the ATR read returns no usable value, stop_distance_pct stays 0 and
    the gate passes open — the pre-trade estimate is best-effort and must
    never block on a missing data feed (the pipeline later aborts cleanly via
    the no-ATR/no-stop rule before any order is placed)."""
    from hermes_trader.agents import executor, risk_gates, memory as memory_mod
    import hermes_trader.event_log as event_log

    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    events_path = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", events_path)
    monkeypatch.setattr(event_log, "EVENTS_FILE", events_path)
    mem = memory_mod.AgentMemory(); mem.load()
    monkeypatch.setattr(memory_mod, "memory", mem)

    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True,
        "leverage": 20, "liquidation_maint_margin_pct": 1.0,
        "sl_buffer_bps": 10.0, "debate_gate": {"enabled": False},
        "news_blackout": {"enabled": False},
        "min_ai_confidence": 0.0, "aligned_min_conf": None,
        "min_trend_score": 0.0, "max_concurrent": 9999,
        "max_total_notional_pct": 100_000.0,
        "max_daily_loss_usd": -1_000_000_000,
        "min_market_volume_usd": 0, "min_hip3_volume_usd": 0,
        "min_short_volume_usd": 0, "max_crypto_long_correlated": 9999,
        "cooldown_min": 0, "counter_regime_min_conf": 0.0,
        "block_counter_trend_bypass": False, "crowded_with_min_conf": 0.0,
        "coin_allowlist": [], "coin_blocklist": [],
        "circuit_breaker": {"consecutive_loss_limit": 0,
                            "coin_daily_loss_pct": 0.0,
                            "max_drawdown_pct": 0.0},
    })
    monkeypatch.setattr(executor, "get_max_leverage", lambda _c: 50)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": 1000.0, "available": 900.0,
                                           "total_ntl": 0.0, "asset_positions": []})

    # Price is available (a live mid is a hard pre-gate requirement for the
    # legacy sizing path); ATR is NOT — the stop-distance estimate then stays
    # 0.0 and the H4 gate passes open rather than blocking on missing data.
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)

    captured = {}
    real_eval = risk_gates.eval_all_gates

    def _spy_eval(ctx, config, *args, **kw):
        captured["ctx"] = ctx
        gate_out = real_eval(ctx, config, *args, **kw)
        captured["gate_out"] = gate_out
        return gate_out

    monkeypatch.setattr(executor, "eval_all_gates", _spy_eval)
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: {"status": "ok", "oid": "x"})

    analysis = {
        "id": "h4test2", "coin": "ETH", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 95.0, "tp_px": 110.0,
        "reasoning": "h4 open-path test",
    }
    executor.maybe_execute(analysis)
    ctx = captured["ctx"]
    assert ctx.stop_distance_pct == 0.0  # no ATR → no estimate → pass open
    assert ctx.entry_px == 100.0  # mid was live and reached the gate
    # The H4 gate itself must not block on a missing estimate (the pipeline
    # later aborts via no_atr_no_stop, but that is downstream of the gates).
    h4 = captured["gate_out"].get("results", {}).get("liquidation_buffer", {})
    assert h4.get("pass") is True
