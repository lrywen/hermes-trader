"""classify_regime_alignment: weak-aligned must not be mislabelled counter-regime."""

from hermes_trader.agents.risk_gates import classify_regime_alignment


def test_weak_aligned_up_long_is_not_counter_regime():
    # SUI 2026-09-27: regime up, long, gate folded weak trend score into
    # counter_trend; direction is still aligned.
    mr = {"counter_trend": True, "weak_trend_score": True,
          "against_funding": False}
    counter, weak = classify_regime_alignment(mr)
    assert counter is False
    assert weak is True


def test_genuine_counter_trend_is_counter_regime():
    mr = {"counter_trend": True, "weak_trend_score": False,
          "against_funding": False}
    counter, weak = classify_regime_alignment(mr)
    assert counter is True
    assert weak is False


def test_against_funding_is_counter_regime_even_when_aligned():
    mr = {"counter_trend": False, "weak_trend_score": False,
          "against_funding": True}
    counter, weak = classify_regime_alignment(mr)
    assert counter is True
    assert weak is False


def test_clean_aligned_is_neither():
    mr = {"counter_trend": False, "weak_trend_score": False,
          "against_funding": False}
    counter, weak = classify_regime_alignment(mr)
    assert counter is False
    assert weak is False


def test_missing_keys_treated_as_false():
    counter, weak = classify_regime_alignment({})
    assert counter is False
    assert weak is False
