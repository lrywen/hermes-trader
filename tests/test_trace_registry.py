"""P1 (audit 2026-10-15): durable end-to-end analysis trace registry.

The analysis that authorised a fill must remain joinable after the in-memory
rolling list (MAX_ANALYSES=200) evicts it. These tests pin the slim projection,
the durable event append, id/trace_id lookup, and verify_trace join coverage
without touching the real events.jsonl (event_log is monkeypatched).
"""
from __future__ import annotations

import hermes_trader.agents.trace_registry as tr

# ── slim projection ─────────────────────────────────────────────────────────

def test_slim_keeps_join_keys_and_compact_fields():
    full = {
        "id": "A1", "trace_id": "T1", "coin": "BTC", "verdict": "LONG",
        "confidence": 0.8, "side": "long", "entry_px": 100.0,
        "stop_px": 97.0, "tp_px": 106.0, "created_at": 123,
        "composite_score": 0.5, "regime": "up", "analyst_model": "m",
        "debate_used": True, "ai_down": False, "nlp_parsed": True,
        "json_parsed": True, "degraded": False, "perception_id": "P1",
        # heavy blobs that must NOT enter the durable ledger:
        "reasoning": "x" * 5000, "news_context": ["n"] * 100,
    }
    slim = tr.slim_analysis(full)
    assert slim["id"] == "A1" and slim["trace_id"] == "T1"
    assert slim["coin"] == "BTC" and slim["confidence"] == 0.8
    assert "reasoning" not in slim and "news_context" not in slim
    assert len(str(slim)) < len(str(full))


def test_slim_missing_join_keys_are_empty_strings():
    slim = tr.slim_analysis({"coin": "X"})
    assert slim["id"] == "" and slim["trace_id"] == ""
    assert slim["coin"] == "X"


# ── durable append + index/lookup (event_log monkeypatched) ──────────────────

class _FakeEventLog:
    def __init__(self):
        self.records = []

    def append(self, event, payload=None, trace_id="", timestamp=None):
        self.records.append({"event": event, "payload": dict(payload or {}),
                             "trace_id": trace_id})
        return True

    def query_events(self, *, event_type=None, start=None, end=None):
        return [{"event": r["event"], "payload": r["payload"],
                 "timestamp": "2026-10-15T00:00:00Z"}
                for r in self.records if event_type is None
                or r["event"] == event_type]


def test_record_and_lookup_by_id_and_trace(monkeypatch):
    fake = _FakeEventLog()
    import hermes_trader.event_log as el
    monkeypatch.setattr(el, "append", fake.append)
    monkeypatch.setattr(el, "query_events", fake.query_events)

    assert tr.record_analysis_event(
        {"id": "A9", "trace_id": "T9", "coin": "ETH", "verdict": "SHORT",
         "confidence": 0.7, "side": "short"}) is True
    assert fake.records[0]["event"] == "analysis"
    assert fake.records[0]["trace_id"] == "A9"

    idx = tr.load_analysis_index()
    assert idx["A9"]["coin"] == "ETH"
    assert idx["T9"]["coin"] == "ETH"  # both keys resolve
    assert tr.lookup_analysis("A9")["verdict"] == "SHORT"
    assert tr.lookup_analysis("") is None
    assert tr.lookup_analysis("nope") is None


def test_record_event_failure_is_swallowed(monkeypatch):
    import hermes_trader.event_log as el

    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(el, "append", boom)
    # must not raise
    assert tr.record_analysis_event({"id": "x"}) is False


# ── verify_trace join coverage ──────────────────────────────────────────────

def _seed(monkeypatch):
    fake = _FakeEventLog()
    import hermes_trader.event_log as el
    monkeypatch.setattr(el, "append", fake.append)
    monkeypatch.setattr(el, "query_events", fake.query_events)
    tr.record_analysis_event({"id": "A1", "trace_id": "T1", "coin": "BTC",
                              "verdict": "LONG", "confidence": 0.9,
                              "side": "long"})
    tr.record_analysis_event({"id": "A2", "trace_id": "T2", "coin": "ETH",
                              "verdict": "SHORT", "confidence": 0.8,
                              "side": "short"})
    return fake


def test_verify_trace_full_and_partial_coverage(monkeypatch):
    _seed(monkeypatch)
    fills = [
        {"coin": "BTC", "trace_id": "A1"},   # matched
        {"coin": "ETH", "trace_id": "T2"},   # matched via trace_id key
        {"coin": "SOL", "trace_id": "OLD"},  # key present but evicted
        {"coin": "DOGE"},                    # no key at all
    ]
    rep = tr.verify_trace(fills)
    assert rep["total"] == 4
    assert rep["with_key"] == 3
    assert rep["keyless"] == 1
    assert rep["matched"] == 2
    assert rep["missing"] == 1
    assert rep["missing_keys"] == ["OLD"]
    assert rep["coverage"] == round(2 / 3, 4)


def test_verify_trace_empty_is_safe():
    rep = tr.verify_trace([])
    assert rep["total"] == 0 and rep["coverage"] == 0.0
    assert rep["matched"] == 0


def test_verify_trace_falls_back_to_analysis_id_field(monkeypatch):
    _seed(monkeypatch)
    fills = [{"coin": "BTC", "analysis_id": "A1"}]  # no trace_id field
    rep = tr.verify_trace(fills)
    assert rep["matched"] == 1 and rep["missing"] == 0


# ── memory.record_analysis emits the durable event ──────────────────────────

def test_memory_record_analysis_persists_durable(monkeypatch):
    import hermes_trader.event_log as el
    seen = []
    monkeypatch.setattr(el, "append",
                        lambda e, payload=None, trace_id="", **k:
                        seen.append((e, payload, trace_id)) or True)
    from hermes_trader.agents.memory import AgentMemory
    mem = AgentMemory()
    before = len(mem._analyses)
    mem.record_analysis({"id": "ZZ1", "trace_id": "TZ", "coin": "AVAX",
                         "verdict": "LONG", "confidence": 0.6})
    assert len(mem._analyses) == before + 1
    kinds = [s[0] for s in seen]
    assert "analysis" in kinds
    ev = next(s for s in seen if s[0] == "analysis")
    assert ev[1]["id"] == "ZZ1" and ev[2] == "ZZ1"
