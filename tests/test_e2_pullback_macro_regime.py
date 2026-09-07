"""E2t — behaviour tests for the pullback-long bypass macro-regime gate (E2).

Wave E (audit 2026-09-06, Q3 / P2-2): the pullback-long bypass in
``executor._runner_entry_block_reason`` used to admit a long purely on the
per-coin 4h TA ``uptrendMomentum`` flag. In a choppy macro regime that flag
fires on a false golden cross and buys the range top. E2 adds a *necessary*
condition that the MACRO regime (BTC / SP500 proxy EMA20/30 + ADX via
``market_regime.detect_regime_with_score``) is also ``"up"``.

The bypass is a pure function of (analysis, config) and the macro lookup is a
lazy in-function import, so it is fully offline-testable by monkeypatching
``market_regime.detect_regime_with_score`` — no network, no live config.

Design contract under test:
  * macro "up" + 4h uptrend + structure   -> bypass ADMITS  ("" )
  * macro chop/neutral/down (or lookup error) -> bypass WITHHELD, the trade
    falls through to the existing "late trend-only chase" veto (fail-closed);
  * ``require_macro_uptrend=false`` restores the pre-E2 behaviour;
  * the macro gate is ANDed with (never a replacement for) the 4h uptrend.
"""

import pytest

from hermes_trader.agents import executor, market_regime
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS
from hermes_trader.agents.executor import _runner_entry_block_reason


# ── helpers ────────────────────────────────────────────────────────────────

def _pb_analysis(**over):
    """An analysis dict that satisfies EVERY pullback-long bypass condition
    (4h uptrend, slow-burn structure, low RSI, no fresh impulse, score above
    the pullback floor but below the runner-gate floor) except the macro regime,
    which is injected per-test via monkeypatch."""
    a = {
        "coin": "TESTCOIN",            # no ":" -> not HIP-3
        "side": "long",
        "confidence": 0.80,
        "ai_confidence_raw": 0.80,     # >= min_confidence 0.70
        "composite_score": 25.0,       # >= pullback min 20, < runner min 30
        "uptrend_momentum_fired": True,
        "downtrend_momentum_fired": False,
        "slow_burn_count": 2,          # >= pullback min_slow_burn 1
        "volume_spike_fired": False,
        "breakout_fired": False,
        "momentum_burst_fired": False,  # -> fresh_impulse False
        "daily_mover_fired": False,
        "whale_signal": False,
        "rsi4h": 55.0,                 # < pullback max_rsi 70, < overbought 75
        # atr4h/ema21_4h/close4h absent -> pb_extension None -> ext guard passes
        "mid": 100.0,
    }
    a.update(over)
    return a


def _pb_config(**pb_over):
    pb = {
        "enabled": True,
        "min_composite": 20.0,
        "max_rsi": 70.0,
        "max_extension_atr": 2.0,
        "min_slow_burn": 1,
        "shadow_mode": False,
        "require_macro_uptrend": True,
    }
    pb.update(pb_over)
    return {
        "runner_entry_gate": {
            "enabled": True,
            "min_confidence": 0.70,
            "min_composite": 30.0,
            "pullback_long": pb,
        }
    }


def _set_macro(monkeypatch, regime, score=0.7):
    monkeypatch.setattr(
        market_regime, "detect_regime_with_score",
        lambda coin, **kw: (regime, score),
    )


_LATE_CHASE = "late trend-only chase"


# ── canonical registration ─────────────────────────────────────────────────

def test_e2_canonical_require_macro_uptrend_default_true():
    pb = CANONICAL_DEFAULTS["runner_entry_gate"]["pullback_long"]
    assert pb["require_macro_uptrend"] is True


# ── macro "up" admits; everything else withholds (fail-closed) ─────────────

def test_e2_bypass_admits_when_macro_up(monkeypatch):
    _set_macro(monkeypatch, "up")
    reason = _runner_entry_block_reason(_pb_analysis(), _pb_config())
    assert reason == ""


@pytest.mark.parametrize("regime", ["chop", "neutral", "down"])
def test_e2_bypass_withheld_when_macro_not_up(monkeypatch, regime):
    _set_macro(monkeypatch, regime)
    reason = _runner_entry_block_reason(_pb_analysis(), _pb_config())
    # Bypass withheld -> falls through to the pre-existing late-chase veto.
    assert _LATE_CHASE in reason


def test_e2_macro_lookup_error_fails_closed(monkeypatch):
    def _boom(coin, **kw):
        raise RuntimeError("regime fetch boom")
    monkeypatch.setattr(market_regime, "detect_regime_with_score", _boom)
    reason = _runner_entry_block_reason(_pb_analysis(), _pb_config())
    assert _LATE_CHASE in reason


# ── kill switch restores pre-E2 behaviour ──────────────────────────────────

def test_e2_require_flag_false_restores_legacy(monkeypatch):
    # Macro is chop, but the gate is disabled -> old behaviour, bypass admits.
    _set_macro(monkeypatch, "chop")
    cfg = _pb_config(require_macro_uptrend=False)
    reason = _runner_entry_block_reason(_pb_analysis(), cfg)
    assert reason == ""


# ── macro gate is ANDed with, not a replacement for, the 4h uptrend ────────

def test_e2_macro_up_without_4h_uptrend_still_blocked(monkeypatch):
    _set_macro(monkeypatch, "up")
    a = _pb_analysis(uptrend_momentum_fired=False)
    reason = _runner_entry_block_reason(a, _pb_config())
    assert reason != ""  # not admitted; no bypass without the 4h uptrend
    assert _LATE_CHASE not in reason  # -> lands on the generic "needs impulse" veto


# ── shadow mode interaction ────────────────────────────────────────────────

def test_e2_shadow_records_when_macro_up(monkeypatch, tmp_path):
    _set_macro(monkeypatch, "up")
    monkeypatch.setattr(executor, "_PULLBACK_SHADOW_FILE",
                        str(tmp_path / "pb.jsonl"))
    cfg = _pb_config(shadow_mode=True)
    reason = _runner_entry_block_reason(_pb_analysis(), cfg)
    assert "pullback-long SHADOW" in reason
    # The record carries the macro regime for reconciliation.
    lines = (tmp_path / "pb.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert '"macro_regime": "up"' in lines[0]


def test_e2_shadow_does_not_record_when_macro_chop(monkeypatch, tmp_path):
    _set_macro(monkeypatch, "chop")
    shadow_file = tmp_path / "pb.jsonl"
    monkeypatch.setattr(executor, "_PULLBACK_SHADOW_FILE", str(shadow_file))
    cfg = _pb_config(shadow_mode=True)
    reason = _runner_entry_block_reason(_pb_analysis(), cfg)
    assert _LATE_CHASE in reason
    assert "pullback-long SHADOW" not in reason
    assert not shadow_file.exists()  # withheld before the shadow-recording branch
