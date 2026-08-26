#!/usr/bin/env python3
"""Local verification of the runner_gate fresh_impulse change.

Tests that breakout ALONE (without volumeSpike) now admits a long signal,
while other gate conditions (confidence, RSI, structure) still apply.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hermes_trader.agents.executor import _runner_entry_block_reason

# Minimal live-like config subset
BASE_CONFIG = {
    "runner_entry_gate": {
        "enabled": True,
        "allow_shorts": True,
        "min_confidence": 0.70,
        "min_composite": 30.0,
        "min_hip3_composite": 50.0,
        "mover_min_confidence": 0.72,
        "mover_min_composite": 20.0,
        "rsi_overbought": 75.0,
        "rsi_oversold": 25.0,
        "max_extension_atr": 2.5,
        "pullback_long": {"enabled": False},
    }
}

def make_analysis(**overrides):
    a = {
        "coin": "TEST",
        "side": "long",
        "confidence": 0.75,
        "ai_confidence_raw": 0.75,
        "composite_score": 35.0,
        "volume_spike_fired": False,
        "breakout_fired": False,
        "momentum_burst_fired": False,
        "daily_mover_fired": False,
        "uptrend_momentum_fired": True,
        "downtrend_momentum_fired": False,
        "slow_burn_count": 1,
        "whale_signal": False,
        "reasoning": "",
        "rsi4h": 55.0,
        "atr4h": 1.0,
        "ema21_4h": 100.0,
        "close4h": 101.0,
    }
    a.update(overrides)
    return a

results = []
def check(name, expected_admitted, **overrides):
    cfg = BASE_CONFIG
    a = make_analysis(**overrides)
    reason = _runner_entry_block_reason(a, cfg)
    admitted = (reason == "")
    status = "PASS" if admitted == expected_admitted else "FAIL"
    results.append((status, name, reason))
    print(f"[{status}] {name}")
    print(f"       expected_admitted={expected_admitted}  got_admitted={admitted}")
    if reason:
        print(f"       reason: {reason}")
    print()

# === Core change tests ===

# 1. breakout ALONE, no volumeSpike — should now ADMIT (was blocked before)
check("breakout_only_no_volume_ADMITS",
      expected_admitted=True,
      breakout_fired=True, volume_spike_fired=False,
      composite_score=35.0, slow_burn_count=1)

# 2. breakout ALONE with low score but slow structure — should ADMIT
check("breakout_only_low_score_with_slow_ADMITS",
      expected_admitted=True,
      breakout_fired=True, volume_spike_fired=False,
      composite_score=22.0, slow_burn_count=1)

# 3. breakout + low score + NO slow structure — still needs structure
check("breakout_only_no_structure_BLOCKS",
      expected_admitted=False,
      breakout_fired=True, volume_spike_fired=False,
      composite_score=22.0, slow_burn_count=0)

# 4. volume+burst (the classic combo) — still admits
check("volume_plus_burst_ADMITS",
      expected_admitted=True,
      volume_spike_fired=True, momentum_burst_fired=True,
      breakout_fired=False, composite_score=35.0, slow_burn_count=1)

# 5. burst+high score (no volume) — still admits
check("burst_high_score_ADMITS",
      expected_admitted=True,
      momentum_burst_fired=True, volume_spike_fired=False,
      breakout_fired=False, composite_score=35.0, slow_burn_count=1)

# 6. burst+low score (no volume, no breakout) — blocks
check("burst_low_score_BLOCKS",
      expected_admitted=False,
      momentum_burst_fired=True, volume_spike_fired=False,
      breakout_fired=False, composite_score=20.0, slow_burn_count=1)

# 7. no impulse at all (trend-only) — blocks as late chase
check("trend_only_BLOCKS",
      expected_admitted=False,
      breakout_fired=False, volume_spike_fired=False,
      momentum_burst_fired=False, composite_score=35.0, slow_burn_count=1)

# 8. breakout but confidence too low — still blocks
check("breakout_low_confidence_BLOCKS",
      expected_admitted=False,
      breakout_fired=True, volume_spike_fired=False,
      ai_confidence_raw=0.55, confidence=0.55,
      composite_score=35.0, slow_burn_count=1)

# 9. breakout but RSI overbought — still blocks
check("breakout_overbought_BLOCKS",
      expected_admitted=False,
      breakout_fired=True, volume_spike_fired=False,
      rsi4h=80.0, composite_score=35.0, slow_burn_count=1)

# 10. breakout but over-extended — still blocks
check("breakout_overextended_BLOCKS",
      expected_admitted=False,
      breakout_fired=True, volume_spike_fired=False,
      atr4h=1.0, ema21_4h=100.0, close4h=105.0,
      composite_score=35.0, slow_burn_count=1)

# 11. daily_mover path still works (no fresh impulse needed)
check("daily_mover_path_ADMITS",
      expected_admitted=True,
      daily_mover_fired=True, breakout_fired=False,
      volume_spike_fired=False, momentum_burst_fired=False,
      ai_confidence_raw=0.80, composite_score=50.0, slow_burn_count=1)

# Summary
passed = sum(1 for s, _, _ in results if s == "PASS")
failed = sum(1 for s, _, _ in results if s == "FAIL")
print(f"=== {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
