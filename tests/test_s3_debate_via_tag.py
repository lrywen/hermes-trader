"""S3 (RCA FARTCOIN 2026-08-26, observation 2): debate-gate `via` provenance.

The research layer produces a verdict either via the native multi-LLM debate
(bull/bear/synth) or via a single-LLM fallback (debate disabled, bull/bear
failed, or synth failed/empty). The risk-layer debate_gate historically tagged
*every* passing gate result ``via="debate_consensus"`` regardless of how the
verdict was produced, so a fallback verdict that cleared the analyst vote was
recorded as a "debate consensus" that never happened (the FARTCOIN 2026-08-26
mislabelling).

GateContext now carries ``debate_used`` (True iff the native debate produced
the research verdict); debate_gate tags a pass ``debate_consensus`` only when
it is True and ``single_fallback`` otherwise. This is observability only — the
pass/fail vote, agree_count and blocked branch are unchanged.
"""

from hermes_trader.agents import risk_gates


def _ctx(**kw):
    """Minimal GateContext that can drive debate_gate to a pass."""
    base = dict(
        confidence=0.9,
        current_positions=[],
        trade_notional_usd=50,
        daily_pnl=0,
        market_volume_24h_usd=1e8,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=1000,
        total_open_notional=0,
        composite_score=50.0,
        momentum_burst_fired=False,
        slow_burn_fired=False,
        whale_signal_fired=False,
    )
    base.update(kw)
    return risk_gates.GateContext(**base)


# A config that clears the analyst vote on this ctx without depending on
# triggers/whale: analyst3_default=True hands the regime vote; news-clean
# (analyst4) and conf>=0.75 (analyst5) are the other two, giving 3/5.
_PASS_CFG = {"debate_gate": {"enabled": True, "analyst3_default": True}}


def test_passing_vote_tags_debate_consensus_when_native_debate_used():
    ctx = _ctx(debate_used=True)
    r = risk_gates.debate_gate(ctx, _PASS_CFG)
    assert r["pass"] is True
    assert r["via"] == "debate_consensus"


def test_passing_vote_tags_single_fallback_when_debate_not_used():
    # The FARTCOIN case: a single-LLM fallback verdict (debate_used defaults
    # False) still clears the analyst vote but must not be called a consensus.
    ctx = _ctx(debate_used=False)
    r = risk_gates.debate_gate(ctx, _PASS_CFG)
    assert r["pass"] is True
    assert r["via"] == "single_fallback"


def test_debate_used_defaults_to_false_and_single_fallback():
    # Manual-order / legacy callers never set debate_used; the safe default is
    # False so a pass is tagged single_fallback rather than a false consensus.
    ctx = _ctx()
    assert ctx.debate_used is False
    r = risk_gates.debate_gate(ctx, _PASS_CFG)
    assert r["pass"] is True
    assert r["via"] == "single_fallback"


def test_disabled_gate_still_reports_debate_disabled():
    ctx = _ctx(debate_used=True)
    r = risk_gates.debate_gate(ctx, {"debate_gate": {"enabled": False}})
    assert r["pass"] is True
    assert r["via"] == "debate_disabled"


def test_blocked_vote_has_no_via_and_keeps_reason():
    # Low-confidence, no triggers/whale, analyst3 default False: only the
    # news-clean vote passes (analyst2 fails: conf 0.4 < 0.7; analyst5 fails:
    # conf 0.4 < 0.75) => 1/5 blocked. The blocked branch is untouched — no
    # via tag, reason intact — regardless of debate_used.
    ctx = _ctx(confidence=0.4, composite_score=10.0, debate_used=True)
    r = risk_gates.debate_gate(ctx, {"debate_gate": {"enabled": True}})
    assert r["pass"] is False
    assert "via" not in r
    assert r["agree_count"] == 1
    assert "multi-agent debate blocked" in r["reason"]


def test_post_init_coerces_debate_used_to_bool():
    # Truthy non-bool provenance (e.g. a 1/0 read off a record) is normalised
    # to a strict bool so the via ternary is deterministic.
    assert _ctx(debate_used=1).debate_used is True
    assert _ctx(debate_used="yes").debate_used is True
    assert _ctx(debate_used=0).debate_used is False
    assert _ctx(debate_used=None).debate_used is False


def test_eval_all_gates_propagates_via_through_gate_results():
    # End-to-end: eval_all_gates runs every gate; the debate sub-result must
    # surface the correct via for the same ctx with/without native debate.
    cfg = {
        "min_ai_confidence": 0.8,
        "max_concurrent": 3,
        "max_trade_notional_usd": 200,
        "max_daily_loss_usd": -100,
        "min_market_volume_usd": 5e6,
        "max_total_notional_pct": 1.0,
        "cooldown_min": 60,
        "debate_gate": {"enabled": True, "analyst3_default": True},
    }
    consensus = risk_gates.eval_all_gates(_ctx(debate_used=True), cfg)
    assert consensus["results"]["debate"]["via"] == "debate_consensus"

    fallback = risk_gates.eval_all_gates(_ctx(debate_used=False), cfg)
    assert fallback["results"]["debate"]["via"] == "single_fallback"
