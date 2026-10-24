"""Tests for the record-only candidate_gate_shadow arm (v11 §2.1, 2026-10-24).

The arm buys ONE isolated research verdict for pre-research candidates whose
composite is in the [40,45) band and writes it to a standalone JSONL. It must
never route, trade, book, or cool down, and must be a strict no-op when
disabled. These tests pin those invariants with an injected stub research fn.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import candidate_gate_shadow as cgs


def _cfg(enabled=True, lo=40.0, hi=45.0, log_path=""):
    return {"runner_entry_gate": {"candidate_gate_shadow": {
        "enabled": enabled, "min_composite": lo, "max_composite": hi,
        "log_path": log_path}}}


def _ta(signal="WEAK", intend_long=True, score=15):
    return {"signal": signal, "score": score,
            "intend_long": intend_long, "intend_short": not intend_long}


@pytest.fixture(autouse=True)
def _reset():
    cgs.reset_seen_for_tests()
    yield
    cgs.reset_seen_for_tests()


def test_disabled_is_strict_noop(tmp_path):
    log = tmp_path / "c.jsonl"
    calls = []

    def research_fn(coin, perception, account_snapshot=None):
        calls.append(coin)
        return {"verdict": "LONG", "confidence": 0.7, "id": "a1"}

    out = cgs.maybe_shadow_research(
        coin="REZ", perception={"composite_score": 42.0}, score=42.0,
        ta=_ta(), held=False, fingerprint="fp1",
        config=_cfg(enabled=False, log_path=str(log)),
        data_dir=str(tmp_path), research_fn=research_fn)
    assert out is None and calls == [] and not log.exists()


def test_band_edges_half_open():
    cfg = _cfg()
    f = lambda s: cgs.in_candidate_band(  # noqa: E731
        score=s, ta_signal="WEAK", intend_long=True, held=False, config=cfg)
    assert f(39.99) is False
    assert f(40.0) is True       # lower bound inclusive
    assert f(44.99) is True
    assert f(45.0) is False      # upper bound exclusive


@pytest.mark.parametrize("signal,intend_long,held", [
    ("REJECTED", True, False),     # late-entry veto respected
    ("WEAK", False, False),        # long-only state
    ("WEAK", True, True),          # held coin close-check excluded
])
def test_predicate_filters(signal, intend_long, held):
    assert cgs.in_candidate_band(
        score=42.0, ta_signal=signal, intend_long=intend_long,
        held=held, config=_cfg()) is False


def test_records_isolated_verdict_and_marks_shadow(tmp_path):
    log = tmp_path / "c.jsonl"

    def research_fn(coin, perception, account_snapshot=None):
        return {"verdict": "LONG", "confidence": 0.72, "side": "long",
                "id": "ana-1", "ai_confidence_raw": 0.7}

    out = cgs.maybe_shadow_research(
        coin="REZ", perception={"composite_score": 42.5}, score=42.5,
        ta=_ta(), held=False, fingerprint="fp-rez",
        config=_cfg(log_path=str(log)), data_dir=str(tmp_path),
        research_fn=research_fn)
    assert out and out["shadow"] is True and out["verdict"] == "LONG"
    rec = json.loads(log.read_text().strip())
    assert rec["coin"] == "REZ" and rec["composite_score"] == 42.5
    assert rec["band_lo"] == 40.0 and rec["band_hi"] == 45.0
    assert rec["verdict"] == "LONG" and rec["research_error"] is None
    # standalone stream: nothing leaks to the main research flow — record carries
    # no routing key and the function returns the record only for inspection.
    assert "executed" not in rec


def test_fingerprint_dedup_pays_once(tmp_path):
    log = tmp_path / "c.jsonl"
    calls = []

    def research_fn(coin, perception, account_snapshot=None):
        calls.append(coin)
        return {"verdict": "PASS", "confidence": 0.5, "id": "x"}

    kw = dict(perception={}, score=42.0, ta=_ta(), held=False,
              fingerprint="dup", config=_cfg(log_path=str(log)),
              data_dir=str(tmp_path), research_fn=research_fn)
    cgs.maybe_shadow_research(coin="REZ", **kw)
    second = cgs.maybe_shadow_research(coin="REZ", **kw)
    assert len(calls) == 1 and second is None
    assert len(log.read_text().strip().splitlines()) == 1


def test_research_error_is_recorded_not_raised(tmp_path):
    log = tmp_path / "c.jsonl"

    def boom(coin, perception, account_snapshot=None):
        raise RuntimeError("llm down")

    out = cgs.maybe_shadow_research(
        coin="REZ", perception={}, score=42.0, ta=_ta(), held=False,
        fingerprint="fp-err", config=_cfg(log_path=str(log)),
        data_dir=str(tmp_path), research_fn=boom)
    assert out is not None and out["verdict"] is None
    assert "llm down" in out["research_error"]
    rec = json.loads(log.read_text().strip())
    assert rec["research_error"].startswith("RuntimeError")


def test_malformed_band_disables(tmp_path):
    # lo >= hi must fail safe to no-op (never an unbounded shadow spend).
    cfg = _cfg(lo=45.0, hi=40.0, log_path=str(tmp_path / "c.jsonl"))
    assert cgs.in_candidate_band(
        score=42.0, ta_signal="WEAK", intend_long=True, held=False,
        config=cfg) is False
