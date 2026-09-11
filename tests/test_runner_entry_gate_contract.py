"""Behavioral contract tests for the executor runner entry gate.

T1 (architecture review 2026-09-11): ``_runner_entry_block_reason`` is the
choke point that keeps ~86-98% of scanned candidates out of execution and was
the source of the "broad late-trend admission" loss mode. It is a pure
decision over an analysis dict + config dict, but it reaches a few side-effect
boundaries (choppy-market overlay, short-only / pullback shadow recorders,
per-coin memory, HIP-3 GEX). Those are stubbed here so the tests pin the
DECISION branches deterministically:

  - gate disabled / missing side;
  - confidence floor judged on the model's PRE-FLOOR ai_confidence_raw (a
    structural override cannot buy its way past the bar);
  - late-entry vetoes: RSI overbought (long) / oversold (short), and EMA21
    over-extension in units of 4h ATR;
  - shorts disabled (base switch) still blocks;
  - short structural requirements (downtrend OR fresh+structure);
  - long: whale-only forced override without fresh impulse is rejected; a
    trend-only chase without fresh impulse/daily-mover is rejected; fresh
    impulse + structure admits; breakout-only admits;
  - pullback-long bypass admits a low-risk pullback only when the MACRO regime
    is "up", and SHADOW mode records-but-still-blocks (fail-closed on a regime
    lookup error).

Tests LOCK EXISTING BEHAVIOR and change no trading logic.
"""

import pytest

from hermes_trader.agents import executor

# ── fixtures / builders ─────────────────────────────────────────────────────

def _gate(**kw):
    """Enabled gate with the defaults the live code assumes."""
    base = {
        "enabled": True,
        "min_confidence": 0.70,
        "min_composite": 30.0,
        "min_hip3_composite": 50.0,
        "rsi_overbought": 75.0,
        "rsi_oversold": 25.0,
        "max_extension_atr": 2.5,
        "allow_shorts": True,
        "mover_min_confidence": 0.80,
        "mover_min_composite": 45.0,
    }
    base.update(kw)
    return {"runner_entry_gate": base}


def _analysis(side="long", **kw):
    base = {"coin": "TST", "side": side, "confidence": 0.80,
            "composite_score": 50.0}
    base.update(kw)
    return base


@pytest.fixture
def isolated_gate(monkeypatch):
    """Neutralize every side-effect boundary the gate can reach:
    overlay resolves to the base switches (no de-risk), shadow recorders are
    spies, macro regime defaults to 'up'."""
    # Overlay: identity — applied knobs equal the supplied base switches.
    monkeypatch.setattr(executor, "evaluate_risk_overlay",
                        lambda cfg: {"posture": "neutral"})
    monkeypatch.setattr(executor, "resolve_applied_knobs",
                        lambda base, cfg, snap: dict(base))
    for name in ("_record_short_only_shadow", "_record_risk_tuning_shadow",
                 "_record_pullback_shadow"):
        monkeypatch.setattr(executor, name, lambda *a, **k: None)
    # pullback macro regime lookup (lazy import inside the function).
    import hermes_trader.agents.market_regime as mr
    monkeypatch.setattr(mr, "detect_regime_with_score",
                        lambda coin: ("up", 1.0))
    return monkeypatch


def _block(analysis, config):
    return executor._runner_entry_block_reason(analysis, config)


# ── 1. enablement / side ────────────────────────────────────────────────────

def test_disabled_gate_allows_everything(isolated_gate):
    assert _block(_analysis(), {"runner_entry_gate": {"enabled": False}}) == ""
    assert _block(_analysis(), {}) == ""


@pytest.mark.parametrize("side", ["", None])
def test_missing_side_is_not_gated(isolated_gate, side):
    # side != "short" and != "long" → the long branch returns "" (not a runner
    # setup; upstream verdict routing owns that decision).
    assert _block(_analysis(side=side), _gate()) == ""


# ── 2. confidence floor (pre-floor raw) ─────────────────────────────────────

def test_confidence_below_floor_blocked(isolated_gate):
    reason = _block(_analysis(confidence=0.60), _gate())
    assert reason.startswith("runner_gate_blocked")
    assert "confidence 0.60" in reason


def test_structural_override_cannot_buy_past_floor(isolated_gate):
    """A structural override rewrites confidence UP to min_ai_confidence, which
    would clear the bar by construction. The gate must judge on
    ai_confidence_raw, so a floored-but-weak model vote stays blocked."""
    a = _analysis(confidence=0.70, ai_confidence_raw=0.55)
    reason = _block(a, _gate(min_confidence=0.70))
    assert "confidence 0.55" in reason


def test_confidence_at_boundary_passes_floor(isolated_gate):
    # 0.70 == min must not be blocked by the confidence check (it then needs
    # structure to admit, so here give it a clean breakout).
    a = _analysis(confidence=0.70, breakout_fired=True)
    assert _block(a, _gate(min_confidence=0.70)) == ""


# ── 3. RSI late-entry veto ──────────────────────────────────────────────────

def test_long_overbought_rsi_blocked(isolated_gate):
    a = _analysis(confidence=0.9, breakout_fired=True, rsi4h=80.0)
    reason = _block(a, _gate())
    assert "overbought" in reason


def test_short_oversold_rsi_blocked(isolated_gate):
    a = _analysis(side="short", confidence=0.9, composite_score=50.0,
                  downtrend_momentum_fired=True, rsi4h=20.0)
    reason = _block(a, _gate())
    assert "oversold" in reason


def test_rsi_boundary_is_inclusive(isolated_gate):
    # rsi == overbought threshold must NOT block (strict >); give no structure
    # so it would otherwise block on late-chase, proving it got PAST the RSI
    # veto (reason mentions chase, not overbought).
    a = _analysis(confidence=0.9, uptrend_momentum_fired=True, rsi4h=75.0)
    reason = _block(a, _gate())
    assert "overbought" not in reason
    assert "late trend-only chase" in reason


# ── 4. EMA21 extension veto ─────────────────────────────────────────────────

def test_long_over_extended_blocked(isolated_gate):
    # close 12.5 ATR above EMA21 > 2.5 cap.
    a = _analysis(confidence=0.9, breakout_fired=True,
                  atr4h=1.0, ema21_4h=100.0, close4h=112.5)
    assert "over-extended long" in _block(a, _gate())


def test_short_over_extended_blocked(isolated_gate):
    # close 12.5 ATR below EMA21 < -2.5 cap.
    a = _analysis(side="short", confidence=0.9, composite_score=50.0,
                  downtrend_momentum_fired=True,
                  atr4h=1.0, ema21_4h=100.0, close4h=87.5)
    assert "over-extended short" in _block(a, _gate())


def test_extension_cap_zero_disables_check(isolated_gate):
    a = _analysis(confidence=0.9, breakout_fired=True,
                  atr4h=1.0, ema21_4h=100.0, close4h=200.0)  # wildly extended
    assert _block(a, _gate(max_extension_atr=0)) == ""


# ── 5. shorts switch ────────────────────────────────────────────────────────

def test_shorts_disabled_blocks(isolated_gate):
    a = _analysis(side="short", confidence=0.95, composite_score=80.0,
                  downtrend_momentum_fired=True)
    assert _block(a, _gate(allow_shorts=False)) == \
        "runner_gate_blocked (shorts disabled)"


# ── 6. short structure ──────────────────────────────────────────────────────

def test_short_needs_downtrend_or_fresh_structure(isolated_gate):
    # High confidence/score but no downtrend, no slow-burn, no fresh impulse.
    a = _analysis(side="short", confidence=0.9, composite_score=80.0)
    assert "short needs" in _block(a, _gate())


def test_short_downtrend_admits(isolated_gate):
    a = _analysis(side="short", confidence=0.9, composite_score=10.0,
                  downtrend_momentum_fired=True)
    assert _block(a, _gate()) == ""


def test_short_fresh_impulse_plus_score_admits(isolated_gate):
    a = _analysis(side="short", confidence=0.9, composite_score=40.0,
                  volume_spike_fired=True, momentum_burst_fired=True)
    assert _block(a, _gate()) == ""


# ── 7. long structure / late chase / whale ──────────────────────────────────

def test_long_late_trend_only_chase_blocked(isolated_gate):
    a = _analysis(confidence=0.9, uptrend_momentum_fired=True)
    assert "late trend-only chase" in _block(a, _gate())


def test_long_needs_fresh_impulse_and_structure(isolated_gate):
    # fresh impulse but no slow-burn and score below min_composite.
    a = _analysis(confidence=0.9, composite_score=10.0, breakout_fired=True)
    reason = _block(a, _gate(min_composite=30.0))
    assert "needs fresh breakout/burst and structure" in reason


def test_long_breakout_alone_admits(isolated_gate):
    """breakout_fired is self-confirming (RVOL>=1.5x + 2 closed bars past the
    edge), and with score clearing min_composite it admits on its own."""
    a = _analysis(confidence=0.9, composite_score=35.0, breakout_fired=True)
    assert _block(a, _gate()) == ""


def test_long_volume_burst_admits(isolated_gate):
    a = _analysis(confidence=0.9, composite_score=35.0,
                  volume_spike_fired=True, momentum_burst_fired=True,
                  slow_burn_count=1)
    assert _block(a, _gate()) == ""


def test_whale_only_forced_override_blocked(isolated_gate):
    a = _analysis(confidence=0.9, whale_signal=True,
                  uptrend_momentum_fired=True,
                  reasoning="upgrade to PASS [structural override]")
    assert "whale-only forced override" in _block(a, _gate())


# ── 8. pullback-long bypass ─────────────────────────────────────────────────

def _pullback_gate(**kw):
    pb = {"enabled": True, "min_composite": 20.0, "max_rsi": 70.0,
          "max_extension_atr": 2.0, "min_slow_burn": 1,
          "require_macro_uptrend": True, "shadow_mode": False}
    pb.update(kw)
    return _gate(pullback_long=pb)


def test_pullback_long_bypass_admits_in_macro_uptrend(isolated_gate):
    """Uptrend + slow-burn backing + decent score, NOT fresh impulse, RSI/ext
    within the stricter pullback guards, macro regime 'up' → admitted."""
    a = _analysis(confidence=0.85, composite_score=40.0,
                  uptrend_momentum_fired=True, slow_burn_count=2,
                  rsi4h=55.0, atr4h=1.0, ema21_4h=100.0, close4h=101.0)
    assert _block(a, _pullback_gate()) == ""


def test_pullback_long_withheld_when_macro_not_up(isolated_gate):
    import hermes_trader.agents.market_regime as mr
    isolated_gate.setattr(mr, "detect_regime_with_score",
                          lambda coin: ("chop", 0.0))
    a = _analysis(confidence=0.85, composite_score=40.0,
                  uptrend_momentum_fired=True, slow_burn_count=2, rsi4h=55.0)
    # Bypass withheld → falls through to the late-chase veto (fail-closed).
    assert "late trend-only chase" in _block(a, _pullback_gate())


def test_pullback_long_shadow_mode_records_but_blocks(isolated_gate):
    calls = []
    isolated_gate.setattr(executor, "_record_pullback_shadow",
                          lambda **k: calls.append(k))
    a = _analysis(confidence=0.85, composite_score=40.0,
                  uptrend_momentum_fired=True, slow_burn_count=2, rsi4h=55.0)
    reason = _block(a, _pullback_gate(shadow_mode=True))
    assert "pullback-long SHADOW" in reason
    assert len(calls) == 1  # recorded exactly once


def test_pullback_long_overbought_not_admitted(isolated_gate):
    # RSI above the pullback cap → bypass condition false → late-chase block.
    a = _analysis(confidence=0.85, composite_score=40.0,
                  uptrend_momentum_fired=True, slow_burn_count=2, rsi4h=72.0)
    assert "late trend-only chase" in _block(a, _pullback_gate())
