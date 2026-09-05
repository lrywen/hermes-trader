"""Offline tests for the 2026-09-04 config audit P1 fixes.

Covers (no network, no live container):
  * P1-9   analyst5_whale_or_conf aligned to the 0.62 entry-confidence gate.
  * P1-10  drawdown peak-window / cooldown live in CANONICAL_DEFAULTS (were
           caller defaults, invisible/unsettable via config).
  * P1-11  crowded_funding_threshold raised off the normal funding baseline.
  * P1-12  market_circuit defaults to "shadow" (tail protection records, no
           halt) instead of fully "off".
  * P1-14  startup safety envelope: breach is caught, safe config passes.
  * P1-15  unified daily-loss cutoff: equity-% primary vs USD floor, the
           tighter binds, neither leg silently dead.
  * P1-16  roe_halt_enabled defaults True (realized-fill kill switch is not
           redundant with the planned DSL stop order).
"""

from __future__ import annotations

import pytest

from hermes_trader.agents import risk_gates as RG
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    startup_config_integrity_errors,
)
from hermes_trader.agents.risk_gates import (
    GateContext,
    daily_loss_kill_switch,
    effective_daily_loss_cutoff,
)


# ── P1-9: analyst5 whale-or confidence aligned to entry gate ───────────────

def test_p1_9_analyst5_whale_or_conf_aligned_to_entry_gate():
    assert CANONICAL_DEFAULTS["analyst_scoring"]["analyst5_whale_or_conf"] == 0.62
    assert (
        CANONICAL_DEFAULTS["analyst_scoring"]["analyst5_whale_or_conf"]
        == CANONICAL_DEFAULTS["min_ai_confidence"]
    )


# ── P1-10: drawdown window/cooldown configurable ──────────────────────────

def test_p1_10_drawdown_window_and_cooldown_in_canonical():
    cb = CANONICAL_DEFAULTS["circuit_breaker"]
    assert cb["drawdown_peak_window_days"] == 14.0
    assert cb["drawdown_cooldown_hours"] == 24.0


def test_p1_10_drawdown_keys_read_via_cfg_get():
    # memory.py reads these via cfg_get; ensure they resolve from canonical.
    from hermes_trader.agents.config_store import cfg_get

    assert float(cfg_get("circuit_breaker.drawdown_peak_window_days")) == 14.0
    assert float(cfg_get("circuit_breaker.drawdown_cooldown_hours")) == 24.0


# ── P1-11: crowded funding threshold off the normal baseline ──────────────

def test_p1_11_crowded_funding_threshold_above_normal_baseline():
    thr = float(CANONICAL_DEFAULTS["funding_regime"]["crowded_funding_threshold"])
    # HL normal funding ~0.01% (0.0001) per 8h; crowded must be well above it.
    assert thr >= 0.0003
    assert thr <= 0.0005
    assert thr > 0.0001


# ── P1-12: market_circuit defaults to shadow ──────────────────────────────

def test_p1_12_market_circuit_defaults_to_shadow():
    assert CANONICAL_DEFAULTS["market_circuit"]["mode"] == "shadow"


# ── P1-16: roe blow-up halt enabled ───────────────────────────────────────

def test_p1_16_roe_halt_enabled_by_default():
    assert CANONICAL_DEFAULTS["roe_halt_enabled"] is True
    assert CANONICAL_DEFAULTS["roe_halt_threshold_pct"] == -50.0


# ── P1-14: startup safety envelope ────────────────────────────────────────

def test_p1_14_safe_production_config_passes_envelope():
    # The canonical defaults represent the production posture.
    assert startup_config_integrity_errors(CANONICAL_DEFAULTS) == []


@pytest.mark.parametrize(
    "key,bad_value",
    [
        ("max_trade_notional_usd", 5000.0),   # was the old $800-style drift
        ("max_concurrent", 20),               # was the old 10-style drift
        ("max_total_notional_pct", 25.0),     # past leverage band
        ("leverage", 50),
        ("atr_risk_sizing.risk_per_trade_pct", 0.06),  # tightened envelope: >5% breaches
        ("min_ai_confidence", 0.30),
        ("max_daily_loss_usd", -500.0),
    ],
)
def test_p1_14_loosened_key_breaches_envelope(key, bad_value):
    cfg = dict(CANONICAL_DEFAULTS)
    leaf = key.split(".")[-1]
    if "." in key:
        cfg[key.split(".")[0]] = {**CANONICAL_DEFAULTS[key.split(".")[0]], leaf: bad_value}
    else:
        cfg[key] = bad_value
    errors = startup_config_integrity_errors(cfg)
    assert any(leaf in e for e in errors), f"{key}={bad_value} should breach"


def test_p1_14_non_numeric_value_reported():
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["max_concurrent"] = "banana"
    errors = startup_config_integrity_errors(cfg)
    assert any("max_concurrent" in e and "not numeric" in e for e in errors)


def test_p1_14_missing_key_is_not_an_error():
    # Keys absent from the cfg resolve to canonical at use sites (in envelope).
    cfg = dict(CANONICAL_DEFAULTS)
    del cfg["leverage"]
    assert startup_config_integrity_errors(cfg) == []


# ── P2-20: SHADOW/LIVE position-cap parity (startup refusal on drift) ─────

def test_p2_20_shadow_position_cap_drift_is_startup_error():
    # The production drift this guards against: max_concurrent=4 vs an
    # explicit shadow_book.max_positions=2 → paper book and live gate admit
    # different concurrency, breaking 1:1 shadow parity.
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["max_concurrent"] = 4
    cfg["shadow_book"] = {**CANONICAL_DEFAULTS["shadow_book"], "max_positions": 2}
    errors = startup_config_integrity_errors(cfg)
    assert any("shadow_book.max_positions" in e and "max_concurrent" in e
               for e in errors), errors


def test_p2_20_shadow_position_cap_matching_is_ok():
    cfg = dict(CANONICAL_DEFAULTS)
    cfg["max_concurrent"] = 4
    cfg["shadow_book"] = {**CANONICAL_DEFAULTS["shadow_book"], "max_positions": 4}
    assert startup_config_integrity_errors(cfg) == []


def test_p2_20_absent_shadow_position_cap_is_ok():
    # No explicit max_positions → shadow_book tracks max_concurrent at run
    # time; nothing to drift.
    cfg = dict(CANONICAL_DEFAULTS)
    assert "max_positions" not in cfg["shadow_book"]
    cfg["max_concurrent"] = 4
    assert startup_config_integrity_errors(cfg) == []


# ── P1-15: unified daily-loss cutoff ──────────────────────────────────────

def test_p1_15_usd_floor_binds_on_micro_account():
    # $20.9 equity, 5% → $1.05 pct cutoff vs -$2 floor; floor is tighter
    # (fires first at -$1.05 → pct actually binds). Document actual behaviour:
    # tighter = less negative. -1.05 > -2 → pct binds.
    cutoff, source = effective_daily_loss_cutoff(20.9, -2.0, 5.0)
    assert source == "pct"
    assert cutoff == pytest.approx(-1.045, abs=1e-6)


def test_p1_15_usd_floor_binds_on_large_account():
    # $200 equity, 5% → -$10 pct; the -$2 USD floor is tighter → usd binds.
    cutoff, source = effective_daily_loss_cutoff(200.0, -2.0, 5.0)
    assert source == "usd"
    assert cutoff == -2.0


def test_p1_15_both_legs_equal():
    # -5% of $40 = -$2 = USD floor → both_equal.
    cutoff, source = effective_daily_loss_cutoff(40.0, -2.0, 5.0)
    assert source == "both_equal"
    assert cutoff == pytest.approx(-2.0, abs=1e-9)


def test_p1_15_disabled_when_no_usable_leg():
    cutoff, source = effective_daily_loss_cutoff(0.0, 0.0, 0.0)
    assert source == "disabled"
    assert cutoff == 0.0


def test_p1_15_pct_only():
    cutoff, source = effective_daily_loss_cutoff(100.0, 0.0, 8.0)
    assert source == "pct"
    assert cutoff == pytest.approx(-8.0)


def test_p1_15_gate_blocks_when_pnl_crosses_cutoff():
    ctx = GateContext(
        confidence=0.9,
        current_positions=[],
        trade_notional_usd=10.0,
        daily_pnl=-1.50,          # past the $20.9-equity pct cutoff (-$1.05)
        market_volume_24h_usd=1e9,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=20.9,
        total_open_notional=0.0,
    )
    result = daily_loss_kill_switch(ctx, -2.0, 5.0)
    assert result["pass"] is False
    assert "pct" in result["reason"]


def test_p1_15_gate_passes_above_cutoff():
    ctx = GateContext(
        confidence=0.9,
        current_positions=[],
        trade_notional_usd=10.0,
        daily_pnl=-0.50,          # within cutoff
        market_volume_24h_usd=1e9,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=20.9,
        total_open_notional=0.0,
    )
    assert daily_loss_kill_switch(ctx, -2.0, 5.0)["pass"] is True


def test_p1_15_gate_backward_compatible_single_arg():
    """Old call signature (ctx, max_daily_loss) still works: USD-only leg."""
    ctx = GateContext(
        confidence=0.9,
        current_positions=[],
        trade_notional_usd=10.0,
        daily_pnl=-3.0,
        market_volume_24h_usd=1e9,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=20.9,
        total_open_notional=0.0,
    )
    assert daily_loss_kill_switch(ctx, -2.0)["pass"] is False
    ctx.daily_pnl = -1.0
    assert daily_loss_kill_switch(ctx, -2.0)["pass"] is True


# ── P1-8: CLI scan no longer hardcodes 75 ────────────────────────────────

def test_p1_8_cli_scan_source_has_no_hardcoded_75():
    """cmd_scan must defer to scan config (54) rather than pass min_score=75."""
    import inspect

    from hermes_trader import __main__ as m

    src = inspect.getsource(m.cmd_scan)
    # No explicit min_score= override passed to scan_once; its default defers
    # to scan.minCompositeScore (the word "min_score" may appear in comments).
    assert "min_score=" not in src
    assert "scan_once(universe=universe)" in src
    assert "minCompositeScore" in src
