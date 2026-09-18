"""P1-1 step ③ — characterization for _ai_zero_confidence_block (S3).

Pins the zero-confidence guard extracted from maybe_execute. The guard must
block ONLY the genuine-failure meaning of confidence=0 (AI down, or text that
yielded neither a JSON block nor an NLP verdict), must defer ai_down+PASS to
the downstream ai_verdict_pass branch, and must fail OPEN for legacy records
that predate the parse-provenance flags.
"""
from __future__ import annotations

from hermes_trader.agents import executor

block = executor._ai_zero_confidence_block


def _a(**over):
    base = {"id": "a1", "coin": "BTC"}
    base.update(over)
    return base


def test_unparseable_zero_conf_pass_is_blocked():
    r = block(_a(verdict="PASS", confidence=0.0,
                 json_parsed=False, nlp_parsed=False), "LIVE")
    assert r is not None
    assert r["executed"] is False and r["mode"] == "LIVE"
    assert r["analysis_id"] == "a1"
    assert r["reason"] == (
        "ai_zero_confidence (unparseable response (no JSON, no NLP verdict))")


def test_ai_down_directional_verdict_is_blocked():
    r = block(_a(verdict="LONG", confidence=0, ai_down=True), "LIVE")
    assert r is not None
    assert r["reason"] == "ai_zero_confidence (ai_down (empty/failed response))"


def test_ai_down_with_pass_is_deferred_not_blocked():
    # The dedicated ai_verdict_pass branch downstream owns ai_down+PASS.
    assert block(_a(verdict="PASS", confidence=0, ai_down=True), "LIVE") is None


def test_clean_structured_zero_conf_pass_flows_through():
    # A real low-conviction JSON verdict is NOT a failure — let gates judge it.
    assert block(_a(verdict="PASS", confidence=0.0,
                    json_parsed=True, nlp_parsed=False, ai_down=False),
                 "LIVE") is None


def test_nlp_extracted_zero_conf_pass_flows_through():
    assert block(_a(verdict="PASS", confidence=0.0,
                    json_parsed=False, nlp_parsed=True), "LIVE") is None


def test_legacy_record_without_parse_flags_fails_open():
    # Pre-flag records / hand-built dicts carry neither key: never block.
    assert block(_a(verdict="PASS", confidence=0.0), "LIVE") is None


def test_nonzero_confidence_never_blocks():
    assert block(_a(verdict="LONG", confidence=0.5), "LIVE") is None
    assert block(_a(verdict="PASS", confidence=0.01,
                    json_parsed=False, nlp_parsed=False), "LIVE") is None


def test_other_verdicts_not_in_guard_set_are_ignored():
    assert block(_a(verdict="CLOSE", confidence=0.0,
                    json_parsed=False, nlp_parsed=False), "LIVE") is None
