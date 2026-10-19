"""P0-2 (2026-10-19): long-only research throttle.

Covers the pure predicate `long_only_should_skip` and the new
`intend_long` / `intend_short` keys that `analyze_perception` must always
return on every exit path.
"""

from hermes_trader.agents.ta_filter import long_only_should_skip


def test_pure_short_intent_is_skipped_when_on():
    ta = {"intend_short": True, "intend_long": False}
    assert long_only_should_skip("DOGE", ta, held_coins=set(), scan_long_only=True) is True


def test_long_intent_is_kept():
    ta = {"intend_short": False, "intend_long": True}
    assert long_only_should_skip("DOGE", ta, set(), True) is False


def test_mixed_intent_is_kept():
    # Any long intent keeps the candidate — only pure short-intent is cut.
    ta = {"intend_short": True, "intend_long": True}
    assert long_only_should_skip("DOGE", ta, set(), True) is False


def test_ambiguous_intent_is_kept():
    ta = {"intend_short": False, "intend_long": False}
    assert long_only_should_skip("DOGE", ta, set(), True) is False


def test_held_coin_is_exempt_even_when_short_intent():
    # Held coins still need the AI close-check regardless of intent.
    ta = {"intend_short": True, "intend_long": False}
    assert long_only_should_skip("DOGE", ta, {"DOGE"}, True) is False


def test_switch_off_never_skips():
    ta = {"intend_short": True, "intend_long": False}
    assert long_only_should_skip("DOGE", ta, set(), scan_long_only=False) is False


def test_held_coins_accepts_iterable_and_none():
    ta = {"intend_short": True, "intend_long": False}
    assert long_only_should_skip("DOGE", ta, ["DOGE"], True) is False
    assert long_only_should_skip("DOGE", ta, None, True) is True


def test_missing_intent_keys_default_to_kept():
    # Defensive: a TA dict without intent keys must not be silently dropped.
    assert long_only_should_skip("DOGE", {}, set(), True) is False


def test_analyze_perception_always_returns_intent_keys():
    """Every analyze_perception exit path must expose the intent flags."""
    from hermes_trader.agents.ta_filter import analyze_perception

    # The insufficient-candle-data early rejection path (no candle patching).
    res = analyze_perception({"coin": "BTC", "composite_score": 50})
    assert "intend_long" in res and "intend_short" in res
