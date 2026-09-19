"""Offline tests for scripts/collect_candles.py (no network)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_trader.data import historical_candles as hc

_REPO = Path(__file__).resolve().parents[1]


def _load_module():
    path = _REPO / "scripts" / "collect_candles.py"
    spec = importlib.util.spec_from_file_location("collect_candles_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cc = _load_module()

STEP = hc.INTERVAL_MS["1h"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "candles.json"))
    yield
    hc.reset_cache()


def _serve(prices_by_t, calls):
    def _post(path, payload, *a, **k):
        calls.append(payload["req"])
        req = payload["req"]
        start, end = int(req["startTime"]), int(req["endTime"])
        return [{"t": t, "o": p, "h": p + 1, "l": p - 1, "c": p, "v": 1.0}
                for t, p in sorted(prices_by_t.items()) if start <= t <= end]
    return _post


def test_select_coins_explicit_dedup():
    out = cc.select_coins(5, explicit=["BTC", "ETH", "BTC", "  ", " eth "])
    assert out == ["BTC", "ETH", "eth"]


def test_plan_spans_aligned_and_chunked():
    start = 1_700_000_000_000
    start -= start % STEP
    end = start + (cc._CHUNK_BARS * 2 + 5) * STEP
    spans = cc.plan_spans(start, end, "1h")
    # 2 full chunks + 1 partial; all boundaries grid-aligned.
    assert len(spans) == 3
    assert spans[0][0] == start
    for s0, s1 in spans:
        assert s0 % STEP == 0 and s1 % STEP == 0
        assert (s1 - s0) // STEP + 1 <= cc._CHUNK_BARS
    assert spans[-1][1] == end - end % STEP
    # contiguous, no gap/overlap
    assert spans[1][0] == spans[0][1] + STEP


def test_collect_populates_cache_and_counts(monkeypatch):
    calls = []
    grid = 1_700_000_000_000
    grid -= grid % STEP
    prices = {grid + i * STEP: 100.0 + i for i in range(10)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    stats = cc.collect(["AAA", "BBB"], ["1h"], grid, grid + 9 * STEP)
    assert stats["spans_planned"] == 2
    assert stats["spans_ok"] == 2
    assert stats["span_errors"] == 0
    assert stats["bars_after"] >= 20
    # each coin got its own request
    assert {c["coin"] for c in calls} == {"AAA", "BBB"}
    # cached bars served on a second run → no new requests
    calls.clear()
    stats2 = cc.collect(["AAA"], ["1h"], grid, grid + 9 * STEP)
    assert stats2["spans_ok"] == 1
    assert calls == []


def test_collect_counts_span_error_without_raising(monkeypatch):
    calls = []

    def _boom(path, payload, *a, **k):
        calls.append(payload["req"])
        raise RuntimeError("network down")

    monkeypatch.setattr(hc, "_http_post", _boom)
    grid = 1_700_000_000_000
    grid -= grid % STEP
    stats = cc.collect(["AAA"], ["1h"], grid, grid + 2 * STEP)
    assert stats["spans_ok"] == 0
    assert stats["span_errors"] == 1
    assert stats["errors"] and "AAA" in stats["errors"][0]


def test_parse_end_ms_forms():
    now_ms = cc._parse_end_ms("now")
    assert now_ms > 1_700_000_000_000
    iso = cc._parse_end_ms("2026-01-01")
    expect = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert iso == expect
    assert cc._parse_end_ms("1767225600000") == 1767225600000
    assert cc._parse_end_ms("1767225600") == 1767225600000  # seconds -> ms


def test_progress_callback_fires(monkeypatch):
    monkeypatch.setattr(hc, "_http_post",
                        _serve({1_700_000_000_000 - 1_700_000_000_000 % STEP
                                + i * STEP: 1.0 for i in range(3)}, []))
    seen = []
    grid = 1_700_000_000_000
    grid -= grid % STEP
    cc.collect(["AAA"], ["1h"], grid, grid + 2 * STEP,
               on_progress=lambda coin, iv, added, total: seen.append((coin, iv)))
    assert seen == [("AAA", "1h")]
