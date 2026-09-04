"""Offline tests for the 2026-09-04 config audit P0 fixes.

Covers (no network, no live container):
  * P0-1  debate per-leg timeouts scale off max_latency_s with NO hard 18/24s
          clamp, and honour optional bull_timeout_s / synth_timeout_s.
  * P0-4  per-trade notional cap tiers by equity (hard $ floor for micro
          accounts, then scales with equity so risk_per_trade_pct binds).
  * P0-5  aligned_min_conf is enabled by default at 0.60 (below the 0.62
          min_ai_confidence) instead of silently null/disabled.
  * P0-6  LLM circuit breaker canonical is 3 failures / 300s cooldown.
  * P0-7  debate max_latency_s and research_llm timeout are unified at 25s.
"""

from __future__ import annotations

from hermes_trader.agents import research as R
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get
from hermes_trader.agents.executor import _tiered_notional_cap


# ── P0-1: debate per-leg timeouts ──────────────────────────────────────────

def _patch_debate_cfg(monkeypatch, **overrides):
    base = {
        "enabled": True,
        "max_latency_s": 25.0,
        "bull_timeout_s": None,
        "synth_timeout_s": None,
    }
    base.update(overrides)
    monkeypatch.setattr(R, "_debate_cfg", lambda: dict(base))


def test_debate_timeouts_scale_with_max_latency_no_hard_clamp(monkeypatch):
    """max_latency_s=25 → bull 0.7×25=17.5, synth 0.92×25=23 (floors respected)."""
    _patch_debate_cfg(monkeypatch, max_latency_s=25.0)
    assert R._debate_per_call_timeout() == 25.0 * 0.7
    assert R._debate_synth_timeout() == 25.0 * 0.92


def test_debate_timeouts_raise_without_18_24_clamp(monkeypatch):
    """A high max_latency_s (40s) must NOT be clamped back to 18/24 (P0-1 root cause)."""
    _patch_debate_cfg(monkeypatch, max_latency_s=40.0)
    assert R._debate_per_call_timeout() == 40.0 * 0.7   # 28.0, would have been 18.0
    assert R._debate_synth_timeout() == 40.0 * 0.92     # 36.8, would have been 24.0


def test_debate_explicit_leg_timeouts_win(monkeypatch):
    """Explicit bull_timeout_s / synth_timeout_s override the fractions."""
    _patch_debate_cfg(
        monkeypatch, max_latency_s=25.0,
        bull_timeout_s=22.0, synth_timeout_s=30.0,
    )
    assert R._debate_per_call_timeout() == 22.0
    assert R._debate_synth_timeout() == 30.0


def test_debate_timeout_floors_guard_degenerate_config(monkeypatch):
    """Tiny max_latency_s still floors at 8s (bull) / 12s (synth)."""
    _patch_debate_cfg(monkeypatch, max_latency_s=1.0)
    assert R._debate_per_call_timeout() == 8.0
    assert R._debate_synth_timeout() == 12.0


def test_debate_explicit_timeout_below_floor_is_raised(monkeypatch):
    """An explicit but dangerously-low leg timeout is raised to the floor."""
    _patch_debate_cfg(monkeypatch, bull_timeout_s=2.0, synth_timeout_s=3.0)
    assert R._debate_per_call_timeout() == 8.0
    assert R._debate_synth_timeout() == 12.0


# ── P0-4: tiered per-trade notional cap ────────────────────────────────────

def test_tiered_cap_disabled_when_base_zero():
    assert _tiered_notional_cap(0.0, 1000.0) == 0.0


def test_tiered_cap_hard_floor_for_micro_account():
    # Production scenario: equity $20.9 < $50 tier → stays at absolute $30.
    assert _tiered_notional_cap(30.0, 20.9) == 30.0


def test_tiered_cap_scales_above_tier():
    # equity $100 >= $50 → cap = max(30, 100*1.5) = 150, letting risk sizing bind.
    assert _tiered_notional_cap(30.0, 100.0) == 150.0


def test_tiered_cap_never_below_base_floor():
    # At exactly the tier boundary equity*1.5 (75) already exceeds base (30).
    assert _tiered_notional_cap(30.0, 50.0) == 75.0
    # A large base cap still wins if equity*1.5 is smaller.
    assert _tiered_notional_cap(500.0, 60.0) == 500.0


# ── P0-5 / P0-6 / P0-7: canonical config values ────────────────────────────

def test_aligned_min_conf_enabled_below_entry_gate():
    # P0-5: was None (feature silently off); now 0.60 < min_ai_confidence 0.62.
    val = cfg_get("aligned_min_conf", config={})
    assert val is not None
    assert 0.58 <= val <= 0.60
    assert val < cfg_get("min_ai_confidence", config={})


def test_llm_circuit_breaker_tightened():
    # P0-6: 3 failures / 300s cooldown (was 5 / 120).
    cb = CANONICAL_DEFAULTS["llm_circuit_breaker"]
    assert cb["fail_threshold"] == 3
    assert cb["cooldown_s"] == 300


def test_debate_and_llm_timeouts_unified_at_25s():
    # P0-7: debate max_latency and research_llm timeout agree at 25s.
    assert CANONICAL_DEFAULTS["debate_research"]["max_latency_s"] == 25.0
    assert CANONICAL_DEFAULTS["research_llm"]["timeout_sec"] == 25.0


def test_research_llm_continuations_capped():
    # P1-13 (done alongside P0): continuations 2 → 1 (no 75s worst case).
    assert CANONICAL_DEFAULTS["research_llm"]["continuations"] == 1


def test_max_total_notional_pct_is_equity_multiple():
    # P0-3: value pinned to production 4 (= 400% of equity, a multiple not pct).
    assert CANONICAL_DEFAULTS["max_total_notional_pct"] == 4.0
