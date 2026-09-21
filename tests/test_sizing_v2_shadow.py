"""Tests for the sizing v2 code-level gray release (off / enforce).

Sizing v2 mirrors the DSL three-layer stop (regime → ATR clamp → ROE/lev cap)
for equal-risk notional; it was historically a pure boolean
(atr_risk_sizing.sizing_v2_enabled, on = enforce immediately). The
observation-only shadow JSONL branch was removed in the 2026-09-21 cleanup;
these tests now cover only:

  * _sizing_v2_config mode resolution: env HERMES_SIZING_V2_MODE > block
    sizing_v2_mode > invalid/missing → off. The legacy boolean
    sizing_v2_enabled was retired (P1-4 Phase 1 step 5) and is ignored.
  * Full maybe_execute wiring: ENFORCE applies the v2 width and the gray cap;
    OFF is byte-identical to the legacy v1 path.

Default mode is OFF: with no config/env, behavior is unchanged.
"""

from __future__ import annotations

from hermes_trader.agents import executor

_ENV_MODE = "HERMES_SIZING_V2_MODE"


# ── config resolution ───────────────────────────────────────────────────────
def test_config_defaults_off(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._sizing_v2_config({})["mode"] == "off"
    assert executor._sizing_v2_config({"atr_risk_sizing": {}})["mode"] == "off"
    # Retired legacy boolean is ignored (false or true).
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_enabled": False}})["mode"] == "off"


def test_config_legacy_boolean_is_retired_and_ignored(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    # P1-4 Phase 1 step 5: sizing_v2_mode is the only switch; the old
    # boolean no longer flips the arm to enforce (dead knob resolves off).
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_enabled": True}})["mode"] == "off"
    # An explicit mode still wins alongside the dead boolean.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "enforce",
                             "sizing_v2_enabled": False}})["mode"] == "enforce"


def test_config_block_mode(monkeypatch):
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "shadow"}})["mode"] == "shadow"
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "ENFORCE"}})["mode"] == "enforce"
    # Mode is the only switch; the retired boolean is ignored.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "shadow",
                             "sizing_v2_enabled": True}})["mode"] == "shadow"


def test_config_env_overrides_everything(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "shadow")
    # Env beats both the block tri-state and the legacy boolean.
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "enforce",
                             "sizing_v2_enabled": True}})["mode"] == "shadow"
    monkeypatch.setenv(_ENV_MODE, "enforce")
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "shadow"}})["mode"] == "enforce"


def test_config_invalid_mode_falls_back_off(monkeypatch):
    monkeypatch.setenv(_ENV_MODE, "bogus")
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_enabled": True}})["mode"] == "off"
    monkeypatch.delenv(_ENV_MODE, raising=False)
    assert executor._sizing_v2_config(
        {"atr_risk_sizing": {"sizing_v2_mode": "nope"}})["mode"] == "off"


# ── full maybe_execute wiring (bot SHADOW mode = paper, no real order) ──────
class _StubMemory:
    """Neutral memory stub: no disk, no state, zero slip/cooldowns/pnl."""

    def __getattr__(self, name):
        def _stub(*_a, **_k):
            return None
        return _stub

    def avg_exit_slip_bps(self, coin, days=None):
        return 0.0

    def avg_exit_slip_bps_side(self, coin, side, days=None, min_samples=None,
                               default_bps=2.0):
        return default_bps, "default"

    def avg_hold_hours_side(self, coin, side, days=None, min_samples=None,
                            default_hours=8.0):
        return default_hours, "default"

    def avg_round_trip_fee_bps(self, coin, days=None, min_samples=None):
        return 0.0

    def loss_cooldown_remaining_min(self, coin):
        return 0

    def get_daily_pnl(self):
        return 0.0

    def peak_daily_pnl(self):
        return 0.0

    def daily_realized_pnl(self):
        return 0.0

    def peak_daily_realized_pnl(self):
        return 0.0

    def get_recent_trades(self, n=10):
        return []

    def track_daily_pnl(self, equity, net_contributions=0.0):
        return None


def _wire_executor(monkeypatch, cfg_extra):
    """Mock every I/O boundary around maybe_execute's sizing section.

    Returns a dict capturing the GateContext (trade_notional_usd) the gates
    see — that is the exact notional that would be ordered.
    """
    from hermes_trader.agents import market_regime, shadow_book
    from hermes_trader.client import hl_client

    monkeypatch.setattr(hl_client, "fetch_funding_history",
                        lambda *_a, **_k: [])

    cfg = {
        "mode": "SHADOW", "enable_crypto": True,
        "leverage": 1,
        "max_trade_notional_usd": 0,
        "max_concurrent": 9999,
        "min_market_volume_usd": 0,
        "min_hip3_volume_usd": 0,
        "min_short_volume_usd": 0,
        "max_total_notional_pct": 50.0,
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
        # Legacy v1 width: min(2.5% top-level, 25% ROE at 1x) = 2.5%. v2 runs
        # the DSL mirror: atr_stop clamps atr% 2.0 * 0.5 mult = 1.0% to the
        # 1.0% floor → effective 1.0% (vs v1 2.5%, the documented 2.5x gap).
        "dsl_exit": {
            "max_loss_pct": 2.5, "max_loss_roe_pct": 25.0,
            "atr_stop": {"enabled": True, "atr_mult": 0.5,
                         "floor_pct": 1.0, "ceiling_pct": 4.0},
        },
        # v2 sizing path; gray cap starts at 10%.
        "atr_risk_sizing": {
            "enabled": True,
            "risk_per_trade_pct": 0.02,
            "sizing_basis": "primary_stop",
            "sizing_v2_cap_pct": 0.1,
        },
    }
    cfg.update(cfg_extra)
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(cfg))
    monkeypatch.setattr(executor, "memory", _StubMemory())
    monkeypatch.setattr(executor, "get_max_leverage", lambda _c: 1)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": 1000.0, "available": 900.0,
                                           "total_ntl": 0.0, "asset_positions": []})
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 2.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
    monkeypatch.setattr(market_regime, "detect_regime", lambda *_a, **_k: "neutral")
    monkeypatch.setattr(executor, "get_atr_hist_mean_pct",
                        lambda *_a, **_k: 2.0)
    captured = {}
    real_eval = executor.eval_all_gates

    def _spy_eval(ctx, config, *args, **kw):
        captured["ctx"] = ctx
        return real_eval(ctx, config, *args, **kw)

    monkeypatch.setattr(executor, "eval_all_gates", _spy_eval)
    monkeypatch.setattr(shadow_book, "shadow_open",
                        lambda **kw: captured.update({"shadow_open": kw}))
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: {"status": "ok", "oid": "x"})
    return captured


def _analysis():
    return {
        "id": "sv2test", "coin": "TEST", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "sizing v2 test",
    }


def test_off_matches_legacy_v1(monkeypatch):
    """OFF: v2 math never runs; sizing is the legacy v1 width."""
    monkeypatch.delenv(_ENV_MODE, raising=False)
    captured = _wire_executor(monkeypatch, {})

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    # v1 width 2.5% → risk $20 / 0.025 = $800, under the 1x lev cap ($1000).
    assert abs(captured["ctx"].trade_notional_usd - 800.0) < 1e-6


def test_enforce_applies_v2_width_and_gray_cap(monkeypatch):
    """ENFORCE: the v2 width drives notional and the gray cap throttles it."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    captured = _wire_executor(monkeypatch, {})

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"

    # v2 width 1.0% → risk $20 / 0.01 = $2000 → 1x lev cap clamps to $1000
    # → 10% gray cap scales to $100.
    assert abs(captured["ctx"].trade_notional_usd - 100.0) < 1e-6
    assert abs(captured["shadow_open"]["size_usd"] - 100.0) < 1e-6


def test_legacy_boolean_alone_keeps_arm_off(monkeypatch):
    """With sizing_v2_mode absent, the retired sizing_v2_enabled=true must
    NOT engage v2: the order sizes on the legacy v1 width (off behavior,
    $800)."""
    monkeypatch.delenv(_ENV_MODE, raising=False)
    captured = _wire_executor(
        monkeypatch,
        {"atr_risk_sizing": {"enabled": True, "risk_per_trade_pct": 0.02,
                             "sizing_basis": "primary_stop",
                             "sizing_v2_enabled": True,
                             "sizing_v2_cap_pct": 0.1}})

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    assert abs(captured["ctx"].trade_notional_usd - 800.0) < 1e-6


def test_enforce_gray_cap_bumps_sub_min_to_floor(monkeypatch):
    """ENFORCE gray floor (Audit 2026-09-08): when the gray cap scales the
    notional below the exchange minimum by MORE than the usual 50% bump-gap,
    the order is still bumped to the minimum instead of rejected."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    captured = _wire_executor(monkeypatch, {})
    monkeypatch.setattr(executor, "min_entry_notional_usd",
                        lambda _c, _m: 200.0)

    res = executor.maybe_execute(_analysis())
    assert res.get("reason") == "shadow_mode_would_execute"
    assert abs(captured["ctx"].trade_notional_usd - 200.0) < 1e-6
    assert abs(captured["shadow_open"]["size_usd"] - 200.0) < 1e-6


def test_non_gray_sub_min_still_rejected_past_gap(monkeypatch):
    """The gray floor must NOT weaken the fail-closed gate for non-gray
    undersizing: with cap_pct=1.0 (no gray scale-down), a notional that sits
    >50% under the exchange minimum is still rejected."""
    monkeypatch.setenv(_ENV_MODE, "enforce")
    captured = _wire_executor(
        monkeypatch,
        {"atr_risk_sizing": {"enabled": True, "risk_per_trade_pct": 0.02,
                             "sizing_basis": "primary_stop",
                             "sizing_v2_cap_pct": 1.0}})
    monkeypatch.setattr(executor, "min_entry_notional_usd",
                        lambda _c, _m: 2000.0)

    res = executor.maybe_execute(_analysis())
    assert res.get("executed") is False
    assert "below_min_order_notional" in res.get("reason", "")
    assert "ctx" not in captured
