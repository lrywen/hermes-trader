"""Characterization tests for the news-blackout flag decision leaf.

_binary_news_flags is a pure gate over the analysis dict's AI-assigned
news_risk and news_context; these tests pin its arming rule and headline
labelling behaviour after the verbatim extraction from maybe_execute.
"""

import pytest

from hermes_trader.agents.executor import _binary_news_flags


def test_negative_with_matching_term_labels_first_headline_containing_it():
    armed, match = _binary_news_flags(
        news_risk="negative",
        news_text="BTC hacks reported|SEC sues exchange",
    )
    assert armed is True
    assert match == "'hacks' in: BTC hacks reported"


def test_term_match_is_case_insensitive():
    armed, match = _binary_news_flags(
        news_risk="NEGATIVE", news_text="FOMC minutes due today")
    assert armed is True
    assert match == "'FOMC' in: FOMC minutes due today"


def test_picks_the_first_pipe_headline_containing_the_term():
    armed, match = _binary_news_flags(
        news_risk="negative",
        news_text="benign headline|another quiet day|CPI surprises lower",
    )
    assert armed is True
    assert match == "'CPI' in: CPI surprises lower"


def test_negative_without_any_term_falls_back_to_truncated_text():
    armed, match = _binary_news_flags(
        news_risk="negative", news_text="some odd unclassified headline")
    assert armed is True
    assert match == "some odd unclassified headline"


def test_negative_without_term_truncates_to_140_chars():
    text = "x" * 200
    armed, match = _binary_news_flags(news_risk="negative", news_text=text)
    assert armed is True
    assert match == "x" * 140


def test_negative_without_term_keeps_exactly_140_chars():
    text = "y" * 140
    armed, match = _binary_news_flags(news_risk="negative", news_text=text)
    assert armed is True
    assert match == "y" * 140


def test_positive_news_never_arms_even_with_adverse_term():
    armed, match = _binary_news_flags(
        news_risk="positive", news_text="hack lawsuit fraud crash")
    assert armed is False
    assert match == ""


def test_none_and_other_risks_do_not_arm():
    assert _binary_news_flags(news_risk=None, news_text="hack") == (False, "")
    assert _binary_news_flags(news_risk="none", news_text="hack") == (False, "")
    assert _binary_news_flags(news_risk="mixed", news_text="hack") == (False, "")


def test_negative_with_empty_text_arms_without_label():
    # news_risk is the AI verdict; an empty headline context still arms but
    # has no representative label to surface.
    armed, match = _binary_news_flags(news_risk="negative", news_text="")
    assert armed is True
    assert match == ""


def test_word_boundary_does_not_match_inside_unrelated_words():
    # "securities" must NOT match the \bsec\b alternative (the \w* suffix
    # family is separate); a genuinely negative verdict with no other term
    # falls back to the truncated-text label.
    armed, match = _binary_news_flags(
        news_risk="negative", news_text="strong securities demand")
    assert armed is True
    assert match == "strong securities demand"


def test_suffix_family_matches_inflected_forms():
    armed, match = _binary_news_flags(
        news_risk="negative", news_text="exchange hacked overnight")
    assert armed is True
    assert match == "'hacked' in: exchange hacked overnight"


def test_headline_is_stripped_before_labelling():
    armed, match = _binary_news_flags(
        news_risk="negative", news_text="  cpi miss spooks market  ")
    assert armed is True
    assert match == "'cpi' in: cpi miss spooks market"


def test_requires_keyword_arguments():
    with pytest.raises(TypeError):
        _binary_news_flags("negative", "hack")  # type: ignore[call-arg]
