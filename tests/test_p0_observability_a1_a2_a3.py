"""Observability-only changes from the 2026-10-21 redundancy/L3-L5 plan.

Covers the zero-trading-behaviour items executed on 2026-10-21:
  A1 runner-gate shadow JSONL defaults onto the persistent HERMES_DATA_DIR
     (falls back to HOME when that dir is absent/unwritable; explicit env wins).
  A2 the shadow_book "already open" skip is logged at INFO (admit->fill funnel).
  A3 debate degradation is a three-state marker (disabled / succeeded /
     fallback) with fallback_reason, instead of an indistinguishable None.
"""
from __future__ import annotations

from hermes_trader.agents import executor, research as R
from hermes_trader.agents import shadow_book


# ── A1: runner-gate shadow file path resolution ───────────────────────────

def test_a1_explicit_env_wins(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.jsonl"
    monkeypatch.setenv("HERMES_RUNNER_GATE_SHADOW_FILE", str(explicit))
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "data"))
    assert executor._default_runner_gate_shadow_file() == str(explicit)


def test_a1_uses_writable_data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.delenv("HERMES_RUNNER_GATE_SHADOW_FILE", raising=False)
    monkeypatch.setenv("HERMES_DATA_DIR", str(data))
    out = executor._default_runner_gate_shadow_file()
    assert out == str(data / "runner_gate_shadow.jsonl")


def test_a1_falls_back_to_home_when_data_dir_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_RUNNER_GATE_SHADOW_FILE", raising=False)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = executor._default_runner_gate_shadow_file()
    assert out.endswith("runner_gate_shadow.jsonl")
    assert ".hermes-trading" in out


# ── A2: "already open" skip is INFO, not DEBUG ────────────────────────────

def test_a2_already_open_logged_at_info():
    src = inspect_source(shadow_book)
    lines = src.splitlines()
    idx = next(i for i, l in enumerate(lines)
               if "skip taker open" in l and "already open" in l)
    # The message f-string sits on the line after the logger.<level>( call.
    assert "logger.info(" in lines[idx - 1]
    # And no debug-level call remains anywhere in that skip branch.
    assert "logger.debug" not in lines[idx - 1]


def inspect_source(mod) -> str:
    import inspect
    return inspect.getsource(mod)


# ── A3: debate three-state marker ─────────────────────────────────────────

def _minimal_parsed():
    return {
        "verdict": "LONG", "confidence": 0.7, "side": "long",
        "entry_px": 100.0, "stop_px": 95.0, "tp_px": 110.0,
        "reasoning": "r", "news_risk": "neutral",
    }


def _build(debate_state, reason=""):
    return R._build_analysis(
        "TST", {"composite_score": 50.0},
        news=None, parsed=_minimal_parsed(), ai_text="",
        debate_used=(debate_state == "succeeded"), trace_id="t1",
        as_of_date=None, fired_names=set(),
        tf1h={}, tf4h={}, c1h=[],
        debate_state=debate_state, debate_fallback_reason=reason,
    )


def test_a3_disabled_state():
    a = _build("disabled")
    assert a["debate_state"] == "disabled"
    assert a["debate_used"] is False
    assert a["debate_fallback"] is False
    assert a["fallback_reason"] == ""


def test_a3_succeeded_state():
    a = _build("succeeded")
    assert a["debate_state"] == "succeeded"
    assert a["debate_used"] is True
    assert a["debate_fallback"] is False


def test_a3_fallback_state_carries_reason():
    a = _build("fallback", reason="bull_bear_failed")
    assert a["debate_state"] == "fallback"
    assert a["debate_used"] is False
    assert a["debate_fallback"] is True
    assert a["fallback_reason"] == "bull_bear_failed"


def test_a3_default_state_is_disabled_for_legacy_callers():
    # _build_analysis callers that don't pass the new kwargs must still be valid.
    a = R._build_analysis(
        "TST", {}, news=None, parsed=_minimal_parsed(), ai_text="",
        debate_used=False, trace_id="t", as_of_date=None,
        fired_names=set(), tf1h={}, tf4h={}, c1h=[],
    )
    assert a["debate_state"] == "disabled"
    assert a["debate_fallback"] is False


def test_a3_debate_research_reports_fallback_reason(monkeypatch):
    """When bull/bear raises, _debate_research returns None AND writes the
    concrete reason into the holder instead of degrading silently."""
    # Enable the debate config path and force the bull leg to raise.
    monkeypatch.setattr(R, "_debate_cfg", lambda: {
        "cache_ttl_s": 0, "parallel": True, "max_latency_s": 1.0,
        "use_structured_output": False,
    })
    monkeypatch.setattr(R, "_debate_per_call_timeout", lambda: 1.0)
    monkeypatch.setattr(R, "_debate_cache_key", lambda *a, **k: "k")

    def _boom(*a, **k):
        raise TimeoutError("simulated leg timeout")

    monkeypatch.setattr(R, "_bull_analysis", _boom)
    monkeypatch.setattr(R, "_bear_analysis", _boom)

    holder: dict = {}
    result = R._debate_research(
        "TST", "ctx", {"id": "p"}, atr_abs=0.01, config={},
        reason_holder=holder,
    )
    assert result is None
    assert holder.get("reason") == "bull_bear_failed"
