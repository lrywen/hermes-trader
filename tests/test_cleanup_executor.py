"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _analysis(**kw):
    base = {"id": "a1", "coin": "BTC", "verdict": "LONG", "side": "long",
            "confidence": 0.70, "composite_score": 30, "entry_px": 100,
            "stop_px": 95, "tp_px": 110, "news_context": "no news"}
    base.update(kw)
    return base


def _exec_baseline(monkeypatch, cfg_overrides=None, state_overrides=None):
    """Patch executor's I/O surface with sane defaults; return (executor, captured).
    `captured` records the size/side passed to place_hl_order on the success path."""
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
        "dsl_exit": {"max_loss_pct": 2.0, "max_loss_roe_pct": 30.0,
                     "protect_pct": 0.5, "retrace_threshold": 0.3,
                     "hard_timeout_minutes": 180.0},
        "debate_gate": {"enabled": False},
    }
    cfg.update(cfg_overrides or {})
    state = {"equity": 1000.0, "available": 500.0, "total_ntl": 0.0,
             "asset_positions": []}
    state.update(state_overrides or {})
    captured = {}

    monkeypatch.setattr(executor, "read_agent_config", lambda: cfg)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xMASTER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: state)
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 2.0)
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 40)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional", lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "set_leverage", lambda c, l: {"ok": True})
    monkeypatch.setattr(executor, "place_hl_trigger_order", lambda *a, **k: {"ok": True})
    # _http_post is imported locally inside maybe_execute (hip3 preflight) —
    # patch at the source module, not on executor.
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda p, pl: {"marginSummary": {"accountValue": "500"}})
    monkeypatch.setattr("hermes_trader.agents.market_regime.detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr("hermes_trader.agents.hyperfeed.market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "regimes_by_class": {}})
    def _place(is_buy, size, mid, coin, **kw):
        captured["is_buy"] = is_buy; captured["size"] = size; captured["coin"] = coin
        return {"ok": True, "order_id": "OID1", "avg_px": mid}
    monkeypatch.setattr(executor, "place_hl_order", _place)
    # H-6 (supplemental audit 2026-08-30): the entry path cross-checks the HL
    # mid against live Binance spot; this fixture's world prices every coin at
    # a synthetic 100.0, which a real network check would (correctly) veto.
    # Stub the safety net to fail-open/checked=False, mirroring how every other
    # external I/O surface in this fixture is isolated.
    monkeypatch.setattr(
        "hermes_trader.client.price_crosscheck.crosscheck_price",
        lambda coin, px: {"ok": True, "checked": False, "reason": "test_stub"})
    monkeypatch.setattr(executor, "register_position", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "track_daily_pnl", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)
    # B-F7: the drawdown gate imports the memory singleton directly (not via
    # executor.memory), so stub the peak it reads too — the no-op track above
    # means no peak is ever recorded in this fixture's world.
    monkeypatch.setattr(executor.memory, "peak_equity", lambda: 0.0)
    # Audit 2026-09-03: the fixed gate now reads the rolling-window peak via
    # rolling_peak_equity() instead of only the all-time peak_equity(); stub
    # it too so a polluted singleton from another test cannot latch this
    # fixture's entries on a drawdown block. peak_equity() staying 0.0 also
    # keeps the legacy one-shot rebase path inert (its legacy_peak > 0 guard).
    monkeypatch.setattr(executor.memory, "rolling_peak_equity",
                        lambda window_days=14.0: 0.0)
    # B-F2/B-F6: same direct-import pattern for the streak / per-coin daily
    # loss readers — no closes happen in the fixture, so all read zero/empty.
    monkeypatch.setattr(executor.memory, "consecutive_losses", lambda coin: 0)
    monkeypatch.setattr(executor.memory,
                        "coin_daily_realized_pnl_pct", lambda coin, sod: 0.0)
    monkeypatch.setattr(executor.memory, "get_recent_trades", lambda n=10: [])
    monkeypatch.setattr(executor.memory, "record_trade", lambda t: None)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xabc")
    return executor, captured, cfg


def _runner_gate_config(**over):
    gate = {
        "enabled": True,
        "min_confidence": 0.70,
        "min_composite": 30.0,
        "allow_shorts": True,
        "rsi_overbought": 75.0,
        "rsi_oversold": 25.0,
        "max_extension_atr": 2.5,
        # These cases isolate the confidence/RSI/extension gates; the P2
        # regime breakout veto is tested separately, so keep it off here.
        "regime_breakout_veto": {"enabled": False},
    }
    gate.update(over)
    return {"runner_entry_gate": gate}


def test_executor_structural_override_promotes_pass_to_long(monkeypatch):
    """When explicitly enabled, composite + slow-burn can upgrade an AI PASS.

    Force paths fail closed by default; this test opts in so the old aggressive
    behavior remains covered without making it the implicit live default.
    """
    from hermes_trader.agents import executor

    # Force a config that exercises ONLY the override path, then fails the
    # next stage so we can verify the upgrade happened without HL calls.
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": False,
        "force_execute_composite": 40, "force_execute_slow_burn_count": 2,
        "composite_force_execute": True,
    })
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {"equity": 0})

    analysis = {
        "id": "test-override",
        "coin": "WLFI",
        "verdict": "PASS",
        "confidence": 0.0,
        "composite_score": 45.0,
        "slow_burn_count": 3,
    }
    res = executor.maybe_execute(analysis)
    # The override happens; then execution fails at equity_unavailable (equity=0).
    # That tells us we passed the PASS-block and reached the equity check.
    assert "equity_unavailable" in (res.get("reason") or "")

    # Without the override conditions (composite below 40), the PASS verdict
    # would never reach the equity check — it would short-circuit elsewhere.
    # Sanity check: low-composite PASS doesn't trigger override.
    analysis2 = {**analysis, "composite_score": 30.0}
    res2 = executor.maybe_execute(analysis2)
    # PASS verdict with no override → would still try to execute (since the
    # executor doesn't directly gate on verdict; it relies on side). With
    # equity=0 it'll hit the same gate. We're just verifying no crash.
    assert isinstance(res2, dict)


def test_runner_gate_judges_override_on_raw_confidence_not_floored():
    """结构性 override 把 confidence 抬到 min_ai_confidence 后，闸门必须仍看原始值。

    否则 min_ai_confidence == min_confidence 时 max(floor, raw) 恒等于门槛，
    置信度这道闸门对所有 override 候选都形同虚设（实盘 CASHCAT conf=0.40 成交）。
    """
    from hermes_trader.agents import executor

    analysis = {
        "coin": "CASHCAT",
        "side": "long",
        "confidence": 0.70,          # 已被 _conf_floor 抬高后的值
        "ai_confidence_raw": 0.40,   # 模型真实置信度
        "composite_score": 44.1,
        "volume_spike_fired": True,
        "momentum_burst_fired": True,
        "slow_burn_count": 1,
    }
    cfg = {"runner_entry_gate": {"enabled": True, "min_confidence": 0.70,
                                 "min_composite": 30, "min_hip3_composite": 50}}

    reason = executor._runner_entry_block_reason(analysis, cfg)

    assert "confidence 0.40 < 0.70" in reason


def test_runner_gate_uses_confidence_when_no_raw_field():
    """非 override 候选没有 ai_confidence_raw，必须回退到 confidence 本身。"""
    from hermes_trader.agents import executor

    analysis = {
        "coin": "BOME",
        "side": "long",
        "confidence": 0.75,
        "composite_score": 44.0,
        "volume_spike_fired": True,
        "breakout_fired": True,
        "slow_burn_count": 1,
    }
    cfg = {"runner_entry_gate": {"enabled": True, "min_confidence": 0.70,
                                 "min_composite": 30, "min_hip3_composite": 50,
                                 "regime_breakout_veto": {"enabled": False}}}

    assert executor._runner_entry_block_reason(analysis, cfg) == ""


def test_executor_whale_signal_overrides_pass_to_long(monkeypatch):
    """When explicitly enabled, whale accumulation can upgrade an AI PASS."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": False,
        "whale_force_execute": True,
    })
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {"equity": 0})
    monkeypatch.setattr(executor, "resolve_decision_regime",
                        lambda analysis, config: "neutral")

    analysis = {
        "id": "whale-override", "coin": "ALT", "verdict": "PASS",
        "confidence": 0.0, "composite_score": 10.0, "slow_burn_count": 0,
        "whale_signal": {"signal": "smart_money_accumulation", "confidence": 0.5},
    }
    res = executor.maybe_execute(analysis)
    # Override fires → upgraded to LONG → reaches equity check (equity=0).
    assert "equity_unavailable" in (res.get("reason") or "")

    # No whale signal + weak composite → no override; PASS stays PASS,
    # the executor doesn't upgrade. (verdict gate is downstream; we just
    # confirm it didn't crash and didn't force a trade via override path.)
    analysis_no_whale = {**analysis, "whale_signal": None}
    res2 = executor.maybe_execute(analysis_no_whale)
    assert isinstance(res2, dict)


def test_maybe_execute_reentry_backstop_blocks_when_live_read_drops_position(monkeypatch):
    """If the live account read returns NO positions but the DSL registry still
    tracks the coin (restart/flaky-read window), re-entry must be blocked — else
    the position pyramids. Regression for the xyz:SP500 stacking incident."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit._active_positions.clear()
    # DSL knows we hold SP500 long, but the live read "forgot" it.
    dsl_exit.register_position("xyz:SP500", "long", 7500.0, leverage=10)
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": True,
        "min_available_margin_pct": 0.0,
    })
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "equity": 1000.0, "available": 1000.0,
        # Per-dex margin fix (2026-06-12): an xyz:* trade reads the xyz dex's
        # own equity/available, so the mock must provide it to reach the
        # re-entry guard this test exercises.
        "dex_equity": {"": 1000.0, "xyz": 1000.0},
        "dex_available": {"": 1000.0, "xyz": 1000.0},
        "total_ntl": 0.0, "asset_positions": [],  # live read dropped the position
    })
    placed = {"n": 0}
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda p, pl: {"marginSummary": {"accountValue": "1000"}})
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 7500.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 100.0)
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 10)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional", lambda c, n, mid: n / mid)
    monkeypatch.setattr("hermes_trader.agents.market_regime.detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr("hermes_trader.agents.hyperfeed.market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "regimes_by_class": {}})
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **k: placed.update(n=placed["n"] + 1) or {"ok": True})
    res = executor.maybe_execute({
        "id": "reentry", "coin": "xyz:SP500", "verdict": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 60.0,
    })
    dsl_exit._active_positions.clear()
    assert res["executed"] is False
    assert placed["n"] == 0  # never pyramided
    # blocked specifically by the re-entry / opposite-direction guard
    blk = str(res.get("blocked_by")) + str(res.get("reason"))
    assert "holding" in blk or "re-entry" in blk or "pyramid" in blk, res


def test_maybe_execute_refuses_when_no_atr(monkeypatch):
    """A coin with no computable ATR (insufficient candle history) must be
    refused, never traded blind — guards force-execute of brand-new HIP-3
    listings where research emits stop_px/tp_px = 0.0."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": False,
        "min_available_margin_pct": 0.0,
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
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional", lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "set_leverage", lambda c, lev: {"ok": True})
    # gates pass
    monkeypatch.setattr(executor, "eval_all_gates",
                        lambda ctx, cfg, lt, **kwargs: {"blocked": False, "results": {}})
    monkeypatch.setattr(executor, "resolve_decision_regime",
                        lambda analysis, config: "neutral")
    # the coin under test: no candle history → ATR 0
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 0.0)
    placed = {"n": 0}
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **k: placed.update(n=placed["n"] + 1) or {"ok": True})

    res = executor.maybe_execute({
        "id": "no-atr", "coin": "NEWCOIN", "verdict": "LONG",
        "side": "long", "confidence": 0.8, "composite_score": 60.0,
    })
    assert res["executed"] is False
    assert "no_atr_no_stop" in res["reason"]
    assert placed["n"] == 0  # never placed an order


def test_whale_size_multiplier_clamps_at_2x():
    """The whale multiplier stacks on the confidence tier but clamps at 2×
    base so a high-conf whale trade can't run away."""
    # Pure arithmetic mirror of the executor sizing logic.
    def sized(conf, whale, base=0.07, whale_mult=1.3):
        if conf >= 0.80:
            m = 1.5
        elif conf >= 0.65:
            m = 1.0
        else:
            m = 0.7
        if whale:
            m = min(m * whale_mult, 2.0)
        return base * m
    # high conf + whale: 1.5 × 1.3 = 1.95 (under 2.0 cap)
    assert abs(sized(0.85, True) - 0.07 * 1.95) < 1e-9
    # mid conf + whale: 1.0 × 1.3 = 1.3
    assert abs(sized(0.70, True) - 0.07 * 1.3) < 1e-9
    # no whale: plain tier
    assert abs(sized(0.85, False) - 0.07 * 1.5) < 1e-9


def test_maybe_execute_pass_without_override_is_clean_noop(monkeypatch):
    """If a PASS reaches maybe_execute but the override doesn't actually hold,
    it must no-op (reason=pass_no_override) — never default to a long order."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "LIVE", "enable_crypto": True,
                                 "whale_force_execute": True})
    # PASS, no whale_signal, weak composite → no override → must no-op safely.
    res = executor.maybe_execute({"id": "x1", "coin": "BTC", "verdict": "PASS",
                                  "composite_score": 10.0, "slow_burn_count": 0})
    assert res["executed"] is False
    assert res["reason"] == "pass_no_override"


def test_maybe_execute_ta_sidestep_can_bypass_runner_gate(monkeypatch):
    from hermes_trader.agents import executor

    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE",
        "enable_crypto": True,
        "enable_hip3": False,
        "min_ai_confidence": 0.70,
        "ta_sidestep_force_execute": True,
        "ta_sidestep_min_slow_burn_count": 1,
        "force_execute_composite": 20,
        "runner_entry_gate": {
            "enabled": True,
            "bypass_sidestep_overrides": True,
        },
    })
    monkeypatch.setattr(executor, "_runner_entry_block_reason",
                        lambda analysis, config: "runner_gate_blocked")
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {"equity": 0})
    monkeypatch.setattr(executor, "resolve_decision_regime",
                        lambda analysis, config: "neutral")

    res = executor.maybe_execute({
        "id": "sidestep-pass",
        "coin": "PURR",
        "verdict": "PASS",
        "confidence": 0.30,
        "composite_score": 0.0,
        "momentum_burst_fired": True,
        "slow_burn_count": 1,
    })

    assert "equity_unavailable" in (res.get("reason") or "")


def test_runner_gate_blocks_hip3_gex_pintrap_longs(monkeypatch):
    from hermes_trader.agents import executor, options_gex

    monkeypatch.setattr(
        options_gex,
        "gex_override_caution",
        lambda *a, **k: (True, "GEX pin-trap: jammed under call wall"),
    )
    analysis = {
        "coin": "xyz:WDC",
        "side": "long",
        "confidence": 0.9,
        "composite_score": 70,
        "volume_spike_fired": True,
        "breakout_fired": True,
        "slow_burn_count": 1,
    }
    cfg = {
        "runner_entry_gate": {"enabled": True, "min_confidence": 0.7,
                              "min_composite": 30, "min_hip3_composite": 50},
        "signal_enforcement": {"enabled": True, "veto": True, "gex_veto": True},
        "gex_signal": {"enabled": True, "shadow_mode": False,
                       "caution_near_wall_pct": 10.0},
    }

    reason = executor._runner_entry_block_reason(analysis, cfg)

    assert "GEX pin-trap" in reason


def test_runner_gate_gex_shadow_mode_does_not_block(monkeypatch):
    from hermes_trader.agents import executor, options_gex

    monkeypatch.setattr(
        options_gex,
        "gex_override_caution",
        lambda *a, **k: (True, "GEX pin-trap: jammed under call wall"),
    )
    analysis = {
        "coin": "xyz:WDC",
        "side": "long",
        "confidence": 0.9,
        "composite_score": 70,
        "volume_spike_fired": True,
        "breakout_fired": True,
        "slow_burn_count": 1,
    }
    cfg = {
        "runner_entry_gate": {"enabled": True, "min_confidence": 0.7,
                              "min_composite": 30, "min_hip3_composite": 50},
        "signal_enforcement": {"enabled": True, "veto": True, "gex_veto": True},
        "gex_signal": {"enabled": True, "shadow_mode": True,
                       "caution_near_wall_pct": 10.0},
    }

    assert executor._runner_entry_block_reason(analysis, cfg) == ""


def test_runner_gate_allows_quality_downtrend_short():
    from hermes_trader.agents import executor

    analysis = {
        "coin": "SOL",
        "side": "short",
        "confidence": 0.74,
        "composite_score": 26,
        "downtrend_momentum_fired": True,
        "slow_burn_count": 0,
    }
    cfg = {
        "runner_entry_gate": {
            "enabled": True,
            "allow_shorts": True,
            "min_confidence": 0.70,
            "min_short_confidence": 0.72,
            "min_short_composite": 25,
        },
    }

    assert executor._runner_entry_block_reason(analysis, cfg) == ""


def test_runner_gate_blocks_weak_short_even_when_shorts_enabled():
    from hermes_trader.agents import executor

    analysis = {
        "coin": "SOL",
        "side": "short",
        "confidence": 0.74,
        "composite_score": 10,
        "downtrend_momentum_fired": False,
        "slow_burn_count": 0,
    }
    cfg = {
        "runner_entry_gate": {
            "enabled": True,
            "allow_shorts": True,
            "min_confidence": 0.70,
            "min_short_confidence": 0.72,
            "min_short_composite": 25,
        },
    }

    reason = executor._runner_entry_block_reason(analysis, cfg)

    assert "short needs downtrend momentum" in reason


def test_maybe_execute_mode_off(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch, {"mode": "OFF"})
    r = ex.maybe_execute(_analysis())
    assert r["executed"] is False and r["reason"] == "mode_off"


def test_maybe_execute_hip3_disabled(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch, {"enable_hip3": False})
    r = ex.maybe_execute(_analysis(coin="xyz:MU"))
    assert r["executed"] is False and "hip3_disabled" in r["reason"]


def test_maybe_execute_crypto_disabled(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch, {"enable_crypto": False})
    r = ex.maybe_execute(_analysis(coin="BTC"))
    assert r["executed"] is False and "crypto_disabled" in r["reason"]


def test_maybe_execute_equity_unavailable(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch, state_overrides={"equity": 0.0})
    r = ex.maybe_execute(_analysis())
    assert r["executed"] is False and "equity_unavailable" in r["reason"]


def test_maybe_execute_insufficient_free_margin(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch, state_overrides={"equity": 1000.0, "available": 50.0})
    r = ex.maybe_execute(_analysis())
    assert r["executed"] is False and "insufficient_free_margin" in r["reason"]


def test_maybe_execute_hip3_underfunded(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch)
    # dex check returns near-zero accountValue (patch the source module)
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda p, pl: {"marginSummary": {"accountValue": "0.0"}})
    r = ex.maybe_execute(_analysis(coin="xyz:MU"))
    assert r["executed"] is False and "hip3_dex_underfunded" in r["reason"]


def test_maybe_execute_success_path(monkeypatch):
    ex, captured, _ = _exec_baseline(monkeypatch)
    r = ex.maybe_execute(_analysis())
    assert r["executed"] is True, r
    assert r["order_id"] == "OID1"
    assert captured["is_buy"] is True
    # notional = equity 1000 × frac 0.10 × lev 10 × conviction(0.70→1.0) = 1000; /mid 100 = 10 coins
    assert abs(captured["size"] - 10.0) < 1e-6


def test_maybe_execute_primary_stop_sizing_uses_dsl_risk(monkeypatch):
    ex, captured, _ = _exec_baseline(
        monkeypatch,
        {
            "leverage": 12,
            "max_trade_notional_usd": 500,
            "atr_risk_sizing": {
                "enabled": True,
                "risk_per_trade_pct": 0.01,
                "sizing_basis": "primary_stop",
            },
            "dsl_exit": {"max_loss_pct": 0.75, "max_loss_roe_pct": 6.0,
                         "protect_pct": 1.5, "retrace_threshold": 0.3,
                         "hard_timeout_minutes": 1800.0},
        },
        {"equity": 250.0, "available": 250.0, "total_ntl": 0.0},
    )
    r = ex.maybe_execute(_analysis(confidence=0.75, composite_score=60))
    assert r["executed"] is True, r
    # risk target = $250 * 1%; primary stop = min(0.75%, 6% ROE / 12x) = 0.5%.
    # $2.50 / 0.5% = $500 notional; price is stubbed at $100.
    assert abs(captured["size"] - 5.0) < 1e-6


def test_maybe_execute_order_failed(monkeypatch):
    ex, _, _ = _exec_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw: {"ok": False, "error": "no match"})
    r = ex.maybe_execute(_analysis())
    assert r["executed"] is False and "order_failed" in r["reason"]


def test_maybe_execute_conviction_sizing_high_conf(monkeypatch):
    """conf >= 0.80 → 1.5× size."""
    ex, captured, _ = _exec_baseline(monkeypatch)
    ex.maybe_execute(_analysis(confidence=0.85))
    # 1000 × 0.10 × 10 × 1.5 / 100 = 15 coins
    assert abs(captured["size"] - 15.0) < 1e-6


def test_maybe_execute_whale_boosts_size(monkeypatch):
    """Whale signal multiplies sizing by 1.3 on top of the conf tier."""
    ex, captured, _ = _exec_baseline(monkeypatch, cfg_overrides={
        "whale_size_multiplier": 1.3,
    })
    ex.maybe_execute(_analysis(confidence=0.70, whale_signal={"confidence": 0.5}))
    # 1000 × 0.10 × 10 × (1.0 × 1.3) / 100 = 13 coins
    assert abs(captured["size"] - 13.0) < 1e-6


def test_parse_conviction_tiers_default_and_malformed():
    from hermes_trader.agents.executor import _DEFAULT_CONVICTION_TIERS, _parse_conviction_tiers
    assert _parse_conviction_tiers(None) == _DEFAULT_CONVICTION_TIERS
    assert _parse_conviction_tiers([]) == _DEFAULT_CONVICTION_TIERS
    # malformed entries → fall back to defaults, never raise
    assert _parse_conviction_tiers([["x", "y"]]) == _DEFAULT_CONVICTION_TIERS
    # non-positive multipliers dropped; remaining sorted highest-threshold-first
    assert _parse_conviction_tiers([[0.5, 0.8], [0.9, 2.0], [0.3, 0]]) == [
        (0.9, 2.0), (0.5, 0.8)]


def test_conviction_multiplier_tier_selection():
    from hermes_trader.agents.executor import _conviction_multiplier
    tiers = [(0.85, 2.0), (0.7, 1.2), (0.0, 0.5)]
    assert _conviction_multiplier(0.90, tiers) == 2.0
    assert _conviction_multiplier(0.85, tiers) == 2.0  # inclusive
    assert _conviction_multiplier(0.72, tiers) == 1.2
    assert _conviction_multiplier(0.10, tiers) == 0.5
    # below every threshold when no 0.0 floor → lowest tier's mult
    assert _conviction_multiplier(0.10, [(0.9, 2.0), (0.7, 1.2)]) == 1.2


def test_maybe_execute_custom_conviction_tiers(monkeypatch):
    """A config-supplied aggressive tier (0.85→2.0×) sizes bigger than default."""
    ex, captured, _ = _exec_baseline(
        monkeypatch,
        cfg_overrides={"conviction_tiers": [[0.85, 2.0], [0.65, 1.0], [0.0, 0.5]]})
    ex.maybe_execute(_analysis(confidence=0.90))
    # 1000 × 0.10 × 10 × 2.0 / 100 = 20 coins (vs 15 under the default 1.5×)
    assert abs(captured["size"] - 20.0) < 1e-6


def test_maybe_execute_default_tiers_unchanged(monkeypatch):
    """Absent conviction_tiers → identical to the prior hardcoded behavior."""
    ex, captured, _ = _exec_baseline(monkeypatch)
    ex.maybe_execute(_analysis(confidence=0.50))  # below 0.65 → 0.7×
    # 1000 × 0.10 × 10 × 0.7 / 100 = 7 coins
    assert abs(captured["size"] - 7.0) < 1e-6


def test_maybe_execute_negative_news_blocks(monkeypatch):
    """AI news_risk='negative' stands the trade down, and the block reason
    surfaces the offending headline for log visibility."""
    ex, _, _ = _exec_baseline(monkeypatch)
    r = ex.maybe_execute(_analysis(
        news_risk="negative",
        news_context="SomeCoin suffers major exploit, $5M drained | other headline"))
    assert r["executed"] is False
    nr = (r.get("gate_results") or {}).get("news") or {}
    assert nr.get("pass") is False
    assert "exploit" in (nr.get("reason") or "").lower()


def test_maybe_execute_positive_news_does_not_block(monkeypatch):
    """An earnings BEAT (news_risk='positive') must NOT block — the old
    keyword gate stood down on the mere word 'earnings'."""
    ex, captured, _ = _exec_baseline(monkeypatch)
    r = ex.maybe_execute(_analysis(
        confidence=0.70,
        news_risk="positive",
        news_context="SomeCoin earnings beat expectations, stock surges"))
    assert r["executed"] is True
    assert (r["gate_results"]["news"]["pass"]) is True


def test_maybe_execute_no_news_risk_does_not_block(monkeypatch):
    """Absent/none news_risk → news gate passes (no keyword false-positives)."""
    ex, _, _ = _exec_baseline(monkeypatch)
    r = ex.maybe_execute(_analysis(
        news_context="Fed meeting next week; SEC mentioned in passing"))
    assert r["executed"] is True


def test_runner_gate_blocks_overbought_long():
    from hermes_trader.agents.executor import _runner_entry_block_reason
    analysis = {
        "coin": "TEST", "side": "long", "confidence": 0.9,
        "ai_confidence_raw": 0.9, "composite_score": 60,
        "volume_spike_fired": True, "breakout_fired": True,
        "rsi4h": 82.0,
    }
    reason = _runner_entry_block_reason(analysis, _runner_gate_config())
    assert "RSI 82 > 75" in reason
    assert "late long chase" in reason


def test_runner_gate_blocks_overextended_long():
    from hermes_trader.agents.executor import _runner_entry_block_reason
    # close 10 ATR above EMA21 -> way over the 2.5x threshold
    analysis = {
        "coin": "TEST", "side": "long", "confidence": 0.9,
        "ai_confidence_raw": 0.9, "composite_score": 60,
        "volume_spike_fired": True, "breakout_fired": True,
        "rsi4h": 55.0,
        "atr4h": 1.0, "ema21_4h": 100.0, "close4h": 110.0,
    }
    reason = _runner_entry_block_reason(analysis, _runner_gate_config())
    assert "extension" in reason and "over-extended long" in reason


def test_runner_gate_allows_healthy_long():
    from hermes_trader.agents.executor import _runner_entry_block_reason
    analysis = {
        "coin": "TEST", "side": "long", "confidence": 0.9,
        "ai_confidence_raw": 0.9, "composite_score": 60,
        "volume_spike_fired": True, "breakout_fired": True,
        "rsi4h": 55.0,
        "atr4h": 1.0, "ema21_4h": 100.0, "close4h": 101.0,
    }
    assert _runner_entry_block_reason(analysis, _runner_gate_config()) == ""


def test_runner_gate_blocks_oversold_short():
    from hermes_trader.agents.executor import _runner_entry_block_reason
    analysis = {
        "coin": "TEST", "side": "short", "confidence": 0.9,
        "ai_confidence_raw": 0.9, "composite_score": 60,
        "downtrend_momentum_fired": True,
        "rsi4h": 18.0,
    }
    reason = _runner_entry_block_reason(analysis, _runner_gate_config())
    assert "RSI 18 < 25" in reason and "late short chase" in reason
