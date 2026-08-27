"""End-to-end tests for the native multi-perspective debate path.

Mocks the OpenRouter transport so the bull / bear / arbiter flow runs in-process
without any network or external HTA container. Covers:
  * happy path (parallel bull+bear → structured synthesis → mapped fields)
  * cache HIT on the second call (no LLM calls)
  * arbiter returns unparseable output → single-LLM fallback (None)
  * both sides empty → single-LLM fallback (None)
  * serial mode also produces a verdict
"""

from __future__ import annotations

import json

import pytest

from hermes_trader.agents import research as R
from hermes_trader.agents.research_schema import ResearchVerdict


# ── helpers ────────────────────────────────────────────────────────────────

def _fake_llm_factory(bull_text: str, bear_text: str, synth_text: str, calls: list):
    """Return a fake _call_openrouter that dispatches on prompt fingerprint.

    IMPORTANT: the arbiter prompt schema text contains the literal "SHORT", so
    role detection MUST check "arbiter" first (matching _debate_role), then
    "LONG specialist", then "sidestep specialist" — a naive "'SHORT' in prompt"
    misroutes the synthesis call into the bear branch.
    """

    def fake(system_prompt, user_message, **kwargs):
        calls.append(system_prompt)
        if "arbiter" in system_prompt:
            return synth_text
        if "LONG specialist" in system_prompt:
            return bull_text
        if "sidestep specialist" in system_prompt:
            return bear_text
        return ""

    return fake


def _synth_json(verdict="SHORT", confidence=0.66, conviction="med",
                stop_pct=0.03, bull_case="OI rising", bear_case="funding hot"):
    v = ResearchVerdict(
        verdict=verdict,
        confidence=confidence,
        conviction=conviction,
        thesis=f"{verdict} thesis",
        bull_case=bull_case,
        bear_case=bear_case,
        suggested_stop_pct=stop_pct if verdict != "PASS" else None,
        key_risks=["risk1", "risk2"],
    )
    return v.model_dump_json()


@pytest.fixture(autouse=True)
def _isolate_debate_cache():
    """Each test gets a fresh TTL cache so HIT/STALE behaviour is explicit."""
    with R._debate_cache_lock:
        R._debate_cache.clear()
    yield
    with R._debate_cache_lock:
        R._debate_cache.clear()


@pytest.fixture
def _enable_debate(monkeypatch):
    """Force debate on with fast timeouts, structured output, parallel."""
    monkeypatch.setattr(
        R, "_debate_cfg",
        lambda: {
            "enabled": True,
            "max_latency_s": 10.0,
            "cache_ttl_s": 300.0,
            "parallel": True,
            "use_structured_output": True,
        },
    )
    monkeypatch.setattr(R, "cfg_get", lambda key, *a, **k: 1.5)


def _perception(mid=100.0):
    return {"id": "perc-1", "mid": mid, "triggers": [], "trace_id": "tr-1"}


# ── tests ──────────────────────────────────────────────────────────────────

def test_debate_happy_path_parallel(monkeypatch, _enable_debate, caplog):
    calls: list = []
    synth = _synth_json(verdict="SHORT", confidence=0.7, stop_pct=0.03)
    fake = _fake_llm_factory(
        bull_text='{"stance":"bullish","confidence":0.8,"arguments":["a","b"]}',
        bear_text='{"stance":"bearish","confidence":0.7,"arguments":["c","d"]}',
        synth_text=synth,
        calls=calls,
    )
    monkeypatch.setattr(R, "_call_openrouter", fake)

    with caplog.at_level("INFO", logger="hermes_trader"):
        fields = R._debate_research(
            "BTC", "perp market data blob", _perception(mid=100.0), atr_abs=2.0
        )

    # Exactly 3 LLM calls: bull, bear, arbiter (parallel still = 3 total).
    assert len(calls) == 3
    assert fields is not None
    assert fields["verdict"] == "SHORT"
    assert fields["side"] == "short"
    assert fields["structured"] is True
    assert fields["json_parsed"] is True
    assert fields["key_risks"] == ["risk1", "risk2"]
    assert fields["bull_case"] == "OI rising"
    assert fields["bear_case"] == "funding hot"
    # stop_pct path: 100 * (1 + 0.03) = 103 for a SHORT; tp stays 0 (ATR not used).
    assert fields["stop_px"] == pytest.approx(103.0)
    assert fields["tp_px"] == 0.0
    # Detailed log lines are present.
    assert any("[debate] RESEARCH-START" in m for m in caplog.messages)
    assert any("bull/bear PARALLEL start" in m for m in caplog.messages)
    assert any("[debate] synth OK" in m for m in caplog.messages)
    assert any("[debate] DEBATE-OK" in m for m in caplog.messages)
    assert any("[debate] cache WRITE" in m for m in caplog.messages)


def test_debate_cache_hit_skips_llm(monkeypatch, _enable_debate):
    calls: list = []
    synth = _synth_json(verdict="LONG", confidence=0.6)
    fake = _fake_llm_factory(
        bull_text="bull", bear_text="bear", synth_text=synth, calls=calls
    )
    monkeypatch.setattr(R, "_call_openrouter", fake)

    perception = _perception(mid=100.0)
    first = R._debate_research("ETH", "ctx", perception, atr_abs=1.0)
    second = R._debate_research("ETH", "ctx", perception, atr_abs=1.0)

    assert first is not None and second is not None
    assert first["verdict"] == "LONG"
    # Second call was served from cache — no additional LLM calls beyond the
    # first bull/bear/arbiter trio.
    assert len(calls) == 3


def test_debate_unparseable_synth_returns_none(monkeypatch, _enable_debate, caplog):
    calls: list = []
    # Structured-mode call returns garbage; unstructured retry also garbage.
    fake = _fake_llm_factory(
        bull_text="bull", bear_text="bear", synth_text="not json at all", calls=calls
    )
    monkeypatch.setattr(R, "_call_openrouter", fake)

    with caplog.at_level("WARNING", logger="hermes_trader"):
        fields = R._debate_research(
            "SOL", "ctx", _perception(mid=50.0), atr_abs=1.0
        )

    assert fields is None
    # synth is a single unstructured call (structured=False fixed for latency;
    # parse_structured extracts JSON from prose) — no structured retry.
    assert sum("arbiter" in c for c in calls) == 1
    assert any("synth PARSE-FAIL" in m for m in caplog.messages)


def test_debate_both_empty_returns_none(monkeypatch, _enable_debate, caplog):
    calls: list = []
    fake = _fake_llm_factory(
        bull_text="", bear_text="", synth_text="should-not-be-called", calls=calls
    )
    monkeypatch.setattr(R, "_call_openrouter", fake)

    with caplog.at_level("WARNING", logger="hermes_trader"):
        fields = R._debate_research(
            "DOGE", "ctx", _perception(mid=0.1), atr_abs=0.01
        )

    assert fields is None
    assert sum("arbiter" in c for c in calls) == 0
    # Empty bull/bear responses raise in _debate_direct, so the debate aborts
    # at the bull/bear stage before the arbiter is ever dispatched.
    assert any("bull/bear FAILED" in m for m in caplog.messages)


def test_debate_serial_mode(monkeypatch, _enable_debate):
    monkeypatch.setattr(
        R, "_debate_cfg",
        lambda: {
            "enabled": True,
            "max_latency_s": 10.0,
            "cache_ttl_s": 0.0,
            "parallel": False,
            "use_structured_output": True,
        },
        raising=False,
    )
    calls: list = []
    synth = _synth_json(verdict="PASS", confidence=0.2, conviction="low")
    fake = _fake_llm_factory(
        bull_text="bull", bear_text="bear", synth_text=synth, calls=calls
    )
    monkeypatch.setattr(R, "_call_openrouter", fake)

    fields = R._debate_research(
        "ARB", "ctx", _perception(mid=1.0), atr_abs=0.05
    )

    assert fields is not None
    assert fields["verdict"] == "PASS"
    assert fields["side"] is None
    assert len(calls) == 3
    # PASS has no stop/target.
    assert fields["stop_px"] == 0.0
    assert fields["tp_px"] == 0.0


def test_debate_atr_stop_when_no_stop_pct(monkeypatch, _enable_debate):
    calls: list = []
    synth = _synth_json(verdict="LONG", confidence=0.5, stop_pct=None)
    fake = _fake_llm_factory(
        bull_text="b", bear_text="s", synth_text=synth, calls=calls
    )
    monkeypatch.setattr(R, "_call_openrouter", fake)

    fields = R._debate_research(
        "AVAX", "ctx", _perception(mid=100.0), atr_abs=2.0
    )
    assert fields is not None
    # ATR path: stop = 100 - 2*1.5 = 97, tp = 100 + 2*1.0 = 102.
    assert fields["stop_px"] == pytest.approx(97.0)
    assert fields["tp_px"] == pytest.approx(102.0)
