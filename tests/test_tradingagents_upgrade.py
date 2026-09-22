"""Tests for the mechanisms absorbed from TradingAgents and their read APIs.

  1. Post-close decision reflection — background LLM review, stored and
     injected into the next prompt; surfaced at GET shadow-arms/reflections.
  2. Debate shadow A/B — bull/bear/arbiter runs in the background for eligible
     candidates as a comparison signal; aggregated at GET shadow-arms/debate-ab.

All LLM/network/persistence is mocked; live memory/events files are untouched.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.agents import reflection as RF
from hermes_trader.agents import research as R
from hermes_trader.agents.memory import AgentMemory
from hermes_trader.agents.system_prompt import build_system_prompt
from hermes_trader.dashboard import register_routes


def _mem() -> AgentMemory:
    m = AgentMemory()
    m.flush = lambda *a, **k: None
    return m


def _close(coin="AAA", side="long", closed_at=123, with_signals=True):
    c = {
        "coin": coin, "side": side, "leverage": 3,
        "entry_px": 100.0, "exit_px": 103.0,
        "hold_minutes": 40.0,
        "realized_pnl_pct": 9.0, "realized_pnl_usd": 2.7,
        "regime_at_entry": "up", "forced_override": False,
        "closed_at": closed_at, "entry_time": 100,
        "entry_slip_bps": 1.0, "exit_slip_bps": -0.5,
        "trace_id": "t1",
    }
    if with_signals:
        c["signals_at_entry"] = {"breakout": True}
        c["enforcement_at_entry"] = {"veto": False, "boost": True}
    return c


@pytest.fixture(autouse=True)
def _isolate_debate_cache():
    with R._debate_cache_lock:
        R._debate_cache.clear()
    yield
    with R._debate_cache_lock:
        R._debate_cache.clear()


# ── mechanism 1: reflection ────────────────────────────────────────────────

def test_generate_reflection_calls_llm(monkeypatch):
    seen = {}

    def fake_llm(sys_p, user_msg, **kw):
        seen["timeout"] = kw.get("timeout")
        return "方向判断正确，趋势对齐良好；入场略晚，下次回调即可介入。"

    monkeypatch.setattr(R, "_call_openrouter", fake_llm)
    out = RF.generate_reflection(_close())
    assert out and "方向判断正确" in out
    assert seen["timeout"] == 20


def test_reflection_skips_uninstrumented_close(monkeypatch):
    called = []
    monkeypatch.setattr(R, "_call_openrouter",
                        lambda *a, **k: called.append(1) or "x")
    c = _close(with_signals=False)
    c["entry_time"] = None
    assert RF.generate_reflection(c) is None
    assert not called


def test_reflection_llm_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(R, "_call_openrouter", boom)
    assert RF.generate_reflection(_close()) is None


def test_attach_reflection_tags_close_and_event(monkeypatch):
    m = _mem()
    row = _close()
    m._closes.append(dict(row))
    events = []
    monkeypatch.setattr("hermes_trader.event_log.append",
                        lambda e, payload=None, trace_id="": events.append(payload))
    import hermes_trader.agents.memory as memory_mod
    monkeypatch.setattr(memory_mod, "memory", m)

    RF._attach(row, "一条复盘教训")

    assert m._closes[0]["reflection"] == "一条复盘教训"
    assert m.get_recent_reflections()[0]["text"] == "一条复盘教训"
    assert events and events[0]["text"] == "一条复盘教训"


def test_prompt_includes_reflection_lessons():
    refls = [{"coin": "AAA", "text": "不要在突破确认后还因为超买而放弃。"}]
    prompt = build_system_prompt("SHADOW", 0.6, 10, refls)
    assert "POST-CLOSE REVIEWS" in prompt and "[AAA]" in prompt


def test_prompt_without_reflections():
    assert "POST-CLOSE REVIEWS" not in build_system_prompt("SHADOW", 0.6, 10, [])


# ── mechanism 2: debate shadow A/B core ───────────────────────────────────

def test_shadow_ab_selection_threshold(monkeypatch):
    monkeypatch.setattr(R, "_debate_shadow_ab_cfg", lambda: {
        "enabled": True, "min_composite": 60.0, "sample_rate": 1.0})
    assert R._shadow_ab_selected({"composite_score": 50, "id": "a"}) is False
    assert R._shadow_ab_selected({"composite_score": 70, "id": "b"}) is True


def test_shadow_ab_disabled(monkeypatch):
    monkeypatch.setattr(R, "_debate_shadow_ab_cfg", lambda: {
        "enabled": False, "min_composite": 0.0, "sample_rate": 1.0})
    assert R._shadow_ab_selected({"composite_score": 90, "id": "a"}) is False


def test_shadow_ab_sampling_deterministic(monkeypatch):
    monkeypatch.setattr(R, "_debate_shadow_ab_cfg", lambda: {
        "enabled": True, "min_composite": 0.0, "sample_rate": 0.25})
    p = {"composite_score": 80, "id": "fixed-id"}
    assert R._shadow_ab_selected(dict(p)) == R._shadow_ab_selected(p)


def test_run_shadow_ab_emits_event(monkeypatch):
    events = []
    monkeypatch.setattr("hermes_trader.event_log.append",
                        lambda e, payload=None, trace_id="": events.append(payload))
    monkeypatch.setattr(
        R, "_debate_research",
        lambda *a, **k: {"verdict": "LONG", "side": "long", "confidence": 0.8})
    single = {"verdict": "LONG", "side": "long", "confidence": 0.72}
    R._run_shadow_ab("AAA", "msg", {"composite_score": 70}, atr_abs=1.0,
                     config=None, single=single)
    ev = events[0]
    assert ev["agree"] is True and ev["debate"]["confidence"] == 0.8


def test_run_shadow_ab_failure_contained(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(R, "_debate_research", boom)
    R._run_shadow_ab("AAA", "msg", {"composite_score": 70}, atr_abs=1.0,
                     config=None, single={"verdict": "LONG"})


# ── read endpoints ─────────────────────────────────────────────────────────

def _client(monkeypatch):
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", "op-tok")
    from hermes_trader import dashboard
    dashboard._TTL_CACHE.clear()
    app = FastAPI()
    register_routes(app)
    return TestClient(app, raise_server_exceptions=False)


def test_debate_ab_endpoint_aggregates(monkeypatch):
    fake = [
        {"timestamp": "2026-09-20T00:00:00Z",
         "payload": {"coin": "AAA", "composite_score": 70,
                     "single": {"verdict": "LONG", "side": "long", "confidence": 0.7},
                     "debate": {"verdict": "LONG", "side": "long", "confidence": 0.8},
                     "agree": True}},
        {"timestamp": "2026-09-21T00:00:00Z",
         "payload": {"coin": "BBB", "composite_score": 65,
                     "single": {"verdict": "LONG", "side": "long", "confidence": 0.7},
                     "debate": {"verdict": "PASS", "side": None, "confidence": 0.4},
                     "agree": False}},
    ]
    monkeypatch.setattr("hermes_trader.event_log.query_events",
                        lambda **k: fake)
    c = _client(monkeypatch)
    r = c.get("/api/dashboard/shadow-arms/debate-ab", params={"days": 30})
    assert r.status_code == 200
    d = r.json()
    assert d["sample"] == 2
    assert d["agreement_rate"] == 0.5
    assert d["single_verdicts"] == {"LONG": 2}
    assert d["debate_verdicts"] == {"LONG": 1, "PASS": 1}
    assert len(d["rows"]) == 2


def test_reflections_endpoint(monkeypatch):
    m = _mem()
    m.attach_reflection("AAA", "long", 1, "教训一")
    m.attach_reflection("BBB", "long", 2, "教训二")
    monkeypatch.setattr("hermes_trader.agents.memory.memory", m, raising=False)
    c = _client(monkeypatch)
    r = c.get("/api/dashboard/shadow-arms/reflections", params={"limit": 20})
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 2
    assert [x["text"] for x in d["reflections"]] == ["教训一", "教训二"]


# ── mechanism 1 wired into SHADOW paper closes ─────────────────────────────

def test_reflection_row_maps_paper_fill():
    from hermes_trader.agents.shadow_book import _reflection_row
    fill = {
        "coin": "AAA", "side": "long", "leverage": 3,
        "entry_px": 100.0, "price": 98.0, "hold_minutes": 6.4,
        "realized_pnl_pct": -5.84, "realized_pnl_usd": -0.29,
        "entry_regime": "up", "ts": 1_700_000_000_000,
        "opened_at": 1_699_999_616_000, "analysis_id": "an-1",
    }
    row = _reflection_row("taker", fill)
    assert row["coin"] == "AAA" and row["exit_px"] == 98.0
    assert row["closed_at"] == 1_700_000_000_000
    assert row["fill_model"] == "taker"
    # opened_at (ms) -> entry_time (epoch seconds); satisfies reviewer gate
    assert row["entry_time"] == pytest.approx(1_699_999_616.0)


def test_paper_close_schedules_reflection(monkeypatch):
    from hermes_trader.agents import shadow_book as SB

    scheduled = []
    monkeypatch.setattr(RF, "maybe_reflect_async",
                        lambda row: scheduled.append(row))
    # isolation: redirect save + force an in-memory book
    monkeypatch.setattr(SB, "_write_atomic", lambda *a, **k: True)
    book = SB.ShadowBook()
    pos = {
        "id": "p1", "coin": "AAA", "side": "long",
        "entry_px": 100.0, "size_usd": 1000.0, "size_coin": 10.0,
        "leverage": 3, "opened_at": SB._now_ms() - 600_000,
        "entry_regime": "up", "analysis_id": "an-1",
    }
    book.state["accounts"]["taker"]["positions"] = [pos]
    fill = book._close_position("taker", pos, 98.0, "max_loss", hold_min=10.0)

    assert fill["coin"] == "AAA"
    assert scheduled and scheduled[0]["coin"] == "AAA"
    assert scheduled[0]["fill_model"] == "taker"
    assert scheduled[0]["entry_time"] is not None


def test_paper_close_reflection_failure_contained(monkeypatch):
    from hermes_trader.agents import shadow_book as SB

    def boom(*a, **k):
        raise RuntimeError("schedule down")

    monkeypatch.setattr(RF, "maybe_reflect_async", boom)
    monkeypatch.setattr(SB, "_write_atomic", lambda *a, **k: True)
    book = SB.ShadowBook()
    pos = {
        "id": "p1", "coin": "AAA", "side": "long",
        "entry_px": 100.0, "size_usd": 1000.0, "size_coin": 10.0,
        "leverage": 3, "opened_at": SB._now_ms(),
        "entry_regime": "up", "analysis_id": "an-1",
    }
    book.state["accounts"]["taker"]["positions"] = [pos]
    # must not raise even if scheduling throws
    fill = book._close_position("taker", pos, 98.0, "max_loss")
    assert fill["coin"] == "AAA"
