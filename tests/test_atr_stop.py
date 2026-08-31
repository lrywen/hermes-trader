"""Wiring proof for the dsl_exit.atr_stop feature.

Asserts the full path: ExitPolicy(atr_stop_*) + entry_atr_pct on the tracker
→ check() actually moves the Phase-1 stop. Two coins with different ATR get
different stops from the same multiplier; clamps and the ROE cap still bind;
disabled flag and missing ATR fall back to the fixed stop; state round-trips.
"""

from __future__ import annotations

import time

import pytest

from hermes_trader.agents.dsl_exit import (
    DSLTracker,
    ExitPolicy,
    _tracker_from_dict,
    _tracker_to_dict,
)


def _policy(**kw) -> ExitPolicy:
    base = dict(max_loss_pct=3.5, max_loss_roe_pct=100.0, protect_pct=1.0,
                retrace_threshold=0.40, hard_timeout_minutes=99999.0,
                atr_stop_enabled=True, atr_stop_mult=1.5,
                atr_stop_floor_pct=1.0, atr_stop_ceiling_pct=4.0,
                stale_flat_timeout_minutes=0.0,
                # H-5 (supplemental audit 2026-08-30): the hard stop wick-guards
                # for hard_stop_confirm_sec (default 1.0s). _stop_pct probes check()
                # in a tight millisecond loop where that window never elapses, so
                # pin it to 0 to keep exercising the immediate-stop width logic.
                hard_stop_confirm_sec=0.0)
    base.update(kw)
    return ExitPolicy(**base)


def _stop_pct(tracker: DSLTracker, entry: float = 100.0) -> float:
    """Find the realized stop width by probing check() with falling marks."""
    for bps in range(1, 1200):  # probe 0.01% .. 12% below entry
        px = entry * (1 - bps / 10000)
        v = tracker.check(px)
        if v.exit:
            assert "max_loss" in v.reason
            return bps / 100
    raise AssertionError("no stop fired within 12%")


def test_different_atr_different_stop_same_mult():
    quiet = DSLTracker("QUIET", "long", 100.0, time.time(), _policy(),
                       leverage=1, entry_atr_pct=1.0)   # 1.5x1.0 = 1.5%
    wild = DSLTracker("WILD", "long", 100.0, time.time(), _policy(),
                      leverage=1, entry_atr_pct=2.0)    # 1.5x2.0 = 3.0%
    s_quiet, s_wild = _stop_pct(quiet), _stop_pct(wild)
    assert abs(s_quiet - 1.5) < 0.06
    assert abs(s_wild - 3.0) < 0.06
    assert s_wild > s_quiet  # same mult, wider stop on the wilder coin


def test_clamps_bind():
    tiny = DSLTracker("TINY", "long", 100.0, time.time(), _policy(),
                      leverage=1, entry_atr_pct=0.2)    # 0.3% -> floor 1.0%
    huge = DSLTracker("HUGE", "long", 100.0, time.time(), _policy(),
                      leverage=1, entry_atr_pct=10.0)   # 15% -> ceiling 4.0%
    assert abs(_stop_pct(tiny) - 1.0) < 0.06
    assert abs(_stop_pct(huge) - 4.0) < 0.06


def test_roe_cap_still_applies_on_top():
    # 3% ATR stop but ROE cap 18% at 10x = 1.8% spot — ROE cap must win.
    t = DSLTracker("LEV", "long", 100.0, time.time(),
                   _policy(max_loss_roe_pct=18.0), leverage=10,
                   entry_atr_pct=2.0)
    assert abs(_stop_pct(t) - 1.8) < 0.06


def test_disabled_or_missing_atr_falls_back_to_fixed():
    off = DSLTracker("OFF", "long", 100.0, time.time(),
                     _policy(atr_stop_enabled=False), leverage=1,
                     entry_atr_pct=2.0)
    no_atr = DSLTracker("NOATR", "long", 100.0, time.time(), _policy(),
                        leverage=1, entry_atr_pct=0.0)
    assert abs(_stop_pct(off) - 3.5) < 0.06
    assert abs(_stop_pct(no_atr) - 3.5) < 0.06


def test_entry_atr_pct_survives_state_roundtrip():
    t = DSLTracker("RT", "short", 50.0, time.time(), _policy(),
                   leverage=3, entry_atr_pct=2.34)
    t2 = _tracker_from_dict(_tracker_to_dict(t))
    assert t2.entry_atr_pct == 2.34
    assert t2.policy.atr_stop_enabled is True
    assert t2.policy.atr_stop_mult == 1.5


def test_noise_band_policy_survives_state_roundtrip():
    pol = _policy(noise_band_enabled=True, noise_band_atr_mult=1.75,
                  consecutive_breaches_required=2)
    t = DSLTracker("NB", "long", 100.0, time.time(), pol,
                   leverage=2, entry_atr_pct=1.2)
    t2 = _tracker_from_dict(_tracker_to_dict(t))
    assert t2.policy.noise_band_enabled is True
    assert t2.policy.noise_band_atr_mult == 1.75
    assert t2.policy.consecutive_breaches_required == 2


def test_parse_verdict_tags_ai_down():
    from hermes_trader.agents.research import parse_verdict
    failed = parse_verdict("", "BTC", {"mid": 100.0})
    assert failed["verdict"] == "PASS" and failed["ai_down"] is True
    ok = parse_verdict('{"verdict": "PASS", "confidence": 0.4}', "BTC", {"mid": 100.0})
    assert ok["ai_down"] is False


# ── P0-1: stop/target must never reach the executor as 0 on a directional call
def test_parse_verdict_atr_fallback_fills_missing_stop_long():
    """LLM omitted stopPx/tpPx (the 73/73 production case) -> derive from ATR."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict(
        '{"verdict":"LONG","confidence":0.7,"side":"long","entryPx":100}',
        "BTC", {"mid": 100.0}, atr_abs=2.0, sl_atr_mult=1.2, tp_atr_mult=1.0,
    )
    assert v["stop_px"] == pytest.approx(97.6)   # 100 - 2.0*1.2
    assert v["tp_px"] == pytest.approx(102.0)    # 100 + 2.0*1.0


def test_parse_verdict_atr_fallback_inverts_for_short():
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict(
        '{"verdict":"SHORT","confidence":0.7,"side":"short","entryPx":100}',
        "BTC", {"mid": 100.0}, atr_abs=2.0, sl_atr_mult=1.5, tp_atr_mult=1.0,
    )
    assert v["stop_px"] == pytest.approx(103.0)  # above entry for a short
    assert v["tp_px"] == pytest.approx(98.0)


def test_parse_verdict_keeps_valid_ai_stop():
    """A sane AI stop wins over the fallback — we only repair, never override."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict(
        '{"verdict":"LONG","confidence":0.7,"entryPx":100,"stopPx":98.5,"tpPx":105}',
        "BTC", {"mid": 100.0}, atr_abs=2.0,
    )
    assert v["stop_px"] == 98.5 and v["tp_px"] == 105


def test_parse_verdict_discards_inverted_stop_and_refills():
    """Stop above entry on a LONG would liquidate instantly — discard + refill."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict(
        '{"verdict":"LONG","confidence":0.7,"entryPx":100,"stopPx":105,"tpPx":95}',
        "BTC", {"mid": 100.0}, atr_abs=2.0, sl_atr_mult=1.0, tp_atr_mult=1.0,
    )
    assert v["stop_px"] == pytest.approx(98.0)
    assert v["tp_px"] == pytest.approx(102.0)


def test_parse_verdict_no_atr_leaves_zero_and_does_not_raise():
    """No ATR available: degrade to 0 rather than inventing a price."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict(
        '{"verdict":"LONG","confidence":0.7,"entryPx":100}',
        "BTC", {"mid": 100.0}, atr_abs=None,
    )
    assert v["stop_px"] == 0.0 and v["tp_px"] == 0.0


def test_parse_verdict_pass_untouched_by_fallback():
    """PASS/CLOSE carry no position, so no stop is synthesised."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict('{"verdict":"PASS","confidence":0.0}', "BTC", {"mid": 100.0}, atr_abs=2.0)
    assert v["stop_px"] == 0.0 and v["tp_px"] == 0.0


def test_parse_verdict_coerces_junk_stop_to_fallback():
    """String/null/negative prices from the LLM must not propagate."""
    from hermes_trader.agents.research import parse_verdict
    for junk in ('"n/a"', 'null', '-5'):
        v = parse_verdict(
            f'{{"verdict":"LONG","confidence":0.7,"entryPx":100,"stopPx":{junk}}}',
            "BTC", {"mid": 100.0}, atr_abs=2.0, sl_atr_mult=1.0,
        )
        assert v["stop_px"] == pytest.approx(98.0), junk


# ── Parse-provenance flags must survive into the analysis record ──────────────
# The executor's zero-confidence guard reads json_parsed/nlp_parsed off the
# analysis dict. On the first deploy of the ai_down flag the same whitelist
# silently dropped it and the guard never fired; these tests pin the contract
# so a conf=0 PASS is never mistaken for an unparseable response.
def test_parse_verdict_flags_structured_zero_confidence_pass():
    """A decoded {"verdict":"PASS","confidence":0} is a real opinion, not junk."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict('{"verdict":"PASS","confidence":0.0}', "BTC", {"mid": 100.0})
    assert v["json_parsed"] is True
    assert v["ai_down"] is False


def test_parse_verdict_flags_unparseable_response():
    """Text with no JSON and no NLP-extractable verdict is a failure."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict("the market is doing market things", "BTC", {"mid": 100.0})
    assert v["json_parsed"] is False
    assert v["nlp_parsed"] is False
    assert v["ai_down"] is False   # text was returned, it just carried no verdict


def test_analysis_record_carries_parse_flags():
    """research.analyze must not drop the flags in its field whitelist."""
    import inspect
    from hermes_trader.agents import research
    src = inspect.getsource(research)
    assert '"nlp_parsed": bool(parsed.get("nlp_parsed"))' in src
    assert '"json_parsed": bool(parsed.get("json_parsed"))' in src


def test_zero_confidence_structured_pass_is_not_blocked_as_unparseable(monkeypatch):
    """The prod-killing case: AI answered PASS/0.0 in clean JSON. The guard must
    let it through to the override/gate logic instead of reporting it broken."""
    from hermes_trader.agents import executor as ex
    monkeypatch.setattr(
        ex, "read_agent_config",
        lambda: {"mode": "LIVE", "enable_crypto": True},
    )
    res = ex.maybe_execute({
        "id": "t-json", "coin": "BTC", "verdict": "PASS", "confidence": 0.0,
        "ai_down": False, "json_parsed": True, "nlp_parsed": False,
    })
    assert res["executed"] is False
    assert "ai_zero_confidence" not in res["reason"]


def test_zero_confidence_unparseable_pass_is_blocked(monkeypatch):
    """Neither JSON nor NLP produced a verdict -> block."""
    from hermes_trader.agents import executor as ex
    monkeypatch.setattr(
        ex, "read_agent_config",
        lambda: {"mode": "LIVE", "enable_crypto": True},
    )
    res = ex.maybe_execute({
        "id": "t-junk", "coin": "BTC", "verdict": "PASS", "confidence": 0.0,
        "ai_down": False, "json_parsed": False, "nlp_parsed": False,
    })
    assert res["executed"] is False
    assert "ai_zero_confidence" in res["reason"]


def test_zero_confidence_legacy_record_without_flags_fails_open(monkeypatch):
    """Analysis records written before the flags existed must not be blocked."""
    from hermes_trader.agents import executor as ex
    monkeypatch.setattr(
        ex, "read_agent_config",
        lambda: {"mode": "LIVE", "enable_crypto": True},
    )
    res = ex.maybe_execute({
        "id": "t-legacy", "coin": "BTC", "verdict": "PASS", "confidence": 0.0,
    })
    assert res["executed"] is False
    assert "ai_zero_confidence" not in res["reason"]


def test_override_blocked_when_ai_down(monkeypatch):
    """A whale-hinted failure-PASS must NOT be upgraded to a blind LONG."""
    from hermes_trader.agents import executor as ex
    monkeypatch.setattr(
        ex, "read_agent_config",
        lambda: {"mode": "LIVE", "enable_crypto": True, "whale_force_execute": True,
                 "override_requires_ai": True},
    )
    res = ex.maybe_execute({
        "id": "t1", "coin": "BTC", "verdict": "PASS", "confidence": 0.0,
        "whale_signal": True, "ai_down": True,
    })
    assert res["executed"] is False
    assert "ai_verdict_pass" in res["reason"]


def test_loss_cooldown_blocks_reentry(monkeypatch):
    """A coin with an active loss cooldown must be refused before any order."""
    from hermes_trader.agents import executor as ex
    import time as _t
    monkeypatch.setattr(
        ex, "read_agent_config",
        lambda: {"mode": "LIVE", "enable_crypto": True, "loss_cooldown_min": 180},
    )
    # Never flush test cooldowns into the LIVE .agent-memory.json (this test
    # once armed a real 60min TON cooldown in production state).
    # B-M12: set_loss_cooldown now force-flushes, so the stub must accept kwargs.
    monkeypatch.setattr(ex.memory, "flush", lambda *a, **k: None)
    ex.memory.set_loss_cooldown("TON", int(_t.time() * 1000 + 60 * 60_000))
    try:
        res = ex.maybe_execute({
            "id": "t2", "coin": "TON", "verdict": "LONG", "side": "long",
            "confidence": 0.9,
        })
        assert res["executed"] is False
        assert "loss_cooldown" in res["reason"]
    finally:
        ex.memory._cooldowns.pop("TON", None)


def test_degraded_read_filter_protects_daily_pnl(monkeypatch):
    """A >25% equity spike within 180s must be IGNORED (partial-dex read);
    the same value re-asserted after 180s must be ACCEPTED (real move)."""
    from hermes_trader.agents.memory import AgentMemory
    m = AgentMemory()
    monkeypatch.setattr(m, "flush", lambda: None)
    m._initialized = True
    m.track_daily_pnl(100.0)          # baseline: SOD=100
    m.track_daily_pnl(99.0)           # normal tick: dailyPnl=-1
    assert round(m.get_daily_pnl(), 2) == -1.0
    m.track_daily_pnl(59.7)           # phantom: -40% in seconds -> ignored
    assert round(m.get_daily_pnl(), 2) == -1.0  # unchanged, kill-switch safe
    m._last_eq_reading_ts -= 200      # pretend 200s passed -> now plausible
    m.track_daily_pnl(59.7)           # sustained -> accepted
    assert round(m.get_daily_pnl(), 2) == -40.3


def test_breakout_force_execute_upgrades_pass(monkeypatch):
    """O'Neil rule: breakout+volume+composite>=40 upgrades a hedged PASS to
    LONG — but NEVER a failure-PASS (ai_down still blocks)."""
    from hermes_trader.agents import executor as ex
    monkeypatch.setattr(
        ex, "read_agent_config",
        lambda: {"mode": "LIVE", "enable_crypto": True,
                 "breakout_force_execute": True, "override_requires_ai": True},
    )
    base = {"id": "bo1", "coin": "XPL", "verdict": "PASS", "confidence": 0.55,
            "composite_score": 45.0, "slow_burn_count": 0,
            "breakout_fired": True, "volume_spike_fired": True}
    # ai_down failure-PASS: refused before any upgrade
    res = ex.maybe_execute({**base, "ai_down": True})
    assert "ai_verdict_pass" in res["reason"]
    # genuine hedged PASS: upgraded → proceeds past the PASS guard to the
    # equity check (same proof pattern as the whale-override test; a
    # non-upgraded PASS would exit earlier with pass_no_override).
    monkeypatch.setattr(ex, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(ex, "fetch_account_state", lambda u, **kw: {
        "equity": 0.0, "available": 0.0, "dex_equity": {"": 0.0},
        "dex_available": {"": 0.0}, "total_ntl": 0.0, "asset_positions": []})
    res2 = ex.maybe_execute({**base, "ai_down": False})
    assert "equity_unavailable" in (res2.get("reason") or "")
    # XPL signature (composite ~0, no breakout trigger): volumeSpike +
    # uptrendMomentum + 1 slow-burn must also qualify post-retune.
    xpl = {**base, "id": "bo2", "breakout_fired": False, "composite_score": 4.6,
           "uptrend_momentum_fired": True, "slow_burn_count": 2, "ai_down": False}
    res3 = ex.maybe_execute(xpl)
    assert "equity_unavailable" in (res3.get("reason") or "")


def test_stale_flat_timeout_cuts_drifters_spares_peakers():
    """8h below protect -> cut; ever-peaked positions exempt; 0=off."""
    import time as _t
    old = _t.time() - 9 * 3600  # 9h ago
    pol = _policy(stale_flat_timeout_minutes=480.0)
    drifter = DSLTracker("DRIFT", "long", 100.0, old, pol, leverage=1,
                         entry_atr_pct=1.0)
    v = drifter.check(99.0)  # never peaked above protect
    assert v.exit and "stale_flat_timeout" in v.reason
    peaker = DSLTracker("PEAK", "long", 100.0, old, pol, leverage=1,
                        entry_atr_pct=1.0)
    peaker.check(102.0)      # armed phase-2 historically
    v2 = peaker.check(100.5)
    assert not (v2.exit and "stale_flat" in v2.reason)
    off = DSLTracker("OFF2", "long", 100.0, old, _policy(), leverage=1,
                     entry_atr_pct=1.0)
    v3 = off.check(99.0)
    assert "stale_flat" not in (v3.reason or "")
