"""P1-1 step ③ — characterization for _structural_override_path (S4).

Pins the structural-override decision + PASS→LONG upgrade extracted from
maybe_execute: the AI-down block refuses a blind upgrade, a signal VETO
blocks it (unless gex_shadow downgrades to log-only mutation), the PASS→LONG
upgrade mutates verdict/side/confidence/reasoning in place, the TA-sidestep
trigger sets the sidestep_override flag, and a non-PASS verdict flows through
unchanged (return None, no mutation).
"""
from __future__ import annotations

import types

from hermes_trader.agents import executor

override_path = executor._structural_override_path


def _od(**over):
    base = {
        "enf": None, "bar": 40.0, "min_slow_burn": 2,
        "whale": False, "slow_burn": True, "breakout": False,
        "composite_strong": False, "ta_sidestep": False,
    }
    base.update(over)
    return base


def _an(**over):
    base = {
        "id": "a1", "coin": "ETH", "verdict": "PASS",
        "confidence": 0.40, "composite_score": 55.0,
        "slow_burn_count": 3, "ai_down": False,
    }
    base.update(over)
    return base


def _patch(monkeypatch):
    monkeypatch.setattr(executor, "_record_force_override_armed",
                        lambda **kw: None)
    monkeypatch.setattr(executor, "get_orderbook_spread",
                        lambda coin: {"ok": False, "error": "offline"})
    import hermes_trader.agents.perception as perception
    monkeypatch.setattr(perception, "extract_fired_triggers",
                        lambda a: ["slow_burn_1h"])


def test_ai_down_block_refuses_blind_upgrade(monkeypatch):
    _patch(monkeypatch)
    an = _an(ai_down=True)
    res = override_path(an, {"override_requires_ai": True}, "LIVE",
                        override_strong=True, od=_od())
    assert res is not None
    assert res["executed"] is False
    assert res["reason"].startswith("ai_verdict_pass")
    # Verdict untouched on the refuse path.
    assert an["verdict"] == "PASS"


def test_signal_veto_blocks_override(monkeypatch):
    _patch(monkeypatch)
    enf = types.SimpleNamespace(veto=True, veto_reason="gex pin-trap")
    res = override_path(_an(), {}, "LIVE", override_strong=True,
                        od=_od(enf=enf))
    assert res is not None
    assert res["reason"] == "signal_veto (gex pin-trap)"


def test_signal_veto_gex_shadow_mutates_and_continues(monkeypatch):
    _patch(monkeypatch)
    enf = types.SimpleNamespace(veto=True, veto_reason="gex pin-trap")
    an = _an(coin="xyz:MU")
    res = override_path(an, {"gex_signal": {"shadow_mode": True}}, "LIVE",
                        override_strong=True, od=_od(enf=enf))
    assert res is None                      # log-only, not blocked
    assert an["signal_veto"] == "gex pin-trap"


def test_upgrade_mutates_verdict_side_and_confidence(monkeypatch):
    _patch(monkeypatch)
    an = _an()
    res = override_path(an, {"min_ai_confidence": 0.70}, "LIVE",
                        override_strong=True, od=_od())
    assert res is None
    assert an["verdict"] == "LONG"
    assert an["side"] == "long"
    assert an["confidence"] == 0.70          # floored up from 0.40
    assert an["ai_confidence_raw"] == 0.40   # original conviction preserved
    assert an["reasoning"].startswith("[structural override] ")


def test_ta_sidestep_sets_flag(monkeypatch):
    _patch(monkeypatch)
    an = _an()
    res = override_path(an, {}, "LIVE", override_strong=True,
                        od=_od(ta_sidestep=True, slow_burn=False))
    assert res is None
    assert an["sidestep_override"] is True


def test_non_pass_flows_through_unchanged(monkeypatch):
    _patch(monkeypatch)
    an = _an(verdict="LONG")
    res = override_path(an, {}, "LIVE", override_strong=True, od=_od())
    assert res is None
    assert an["verdict"] == "LONG"           # no re-mutation
    assert an["confidence"] == 0.40          # untouched confidence
    assert "side" not in an                  # no upgrade touched side