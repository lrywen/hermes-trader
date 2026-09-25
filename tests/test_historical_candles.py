"""Offline tests for the historical candle data layer (no network)."""
from __future__ import annotations

import pytest

from hermes_trader.data import historical_candles as hc

STEP = hc.INTERVAL_MS["1h"]


def _candle(t, o=100.0, h=101.0, l=99.0, c=100.0, v=1.0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hc.reset_cache()
    cache = tmp_path / "candles.json"
    hc.set_cache_file(str(cache))
    yield
    hc.reset_cache()


def _serve(prices_by_t, calls):
    """Fake _http_post: serves candles keyed by exact open-time, records calls."""
    def _post(path, payload, *a, **k):
        calls.append(payload["req"])
        req = payload["req"]
        start, end = int(req["startTime"]), int(req["endTime"])
        return [_candle(t, c=p) for t, p in sorted(prices_by_t.items())
                if start <= t <= end]
    return _post


def test_parse_drops_nonfinite_and_out_of_order():
    raw = [
        _candle(0, c=100.0),
        _candle(STEP, c=float("nan")),
        _candle(STEP, c=101.0),    # duplicate t after the dropped nan row
        _candle(STEP // 2, c=99.0),  # out of order, dropped
        _candle(2 * STEP, c=102.0),
    ]
    bars = hc._parse_rows(raw)
    assert [b.t for b in bars] == [0, STEP, 2 * STEP]
    assert bars[2].c == 102.0


def test_fetch_range_grid_alignment_and_cache_hit(monkeypatch):
    calls = []
    base = 1_700_000_000_000
    grid = base - base % STEP
    prices = {grid + i * STEP: 100.0 + i for i in range(-2, 10)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    bars = hc.fetch_candle_range("AAA", "1h", grid, grid + 3 * STEP)
    assert [b.t for b in bars] == [grid + i * STEP for i in range(4)]
    assert len(calls) == 1
    # Second call fully covered by cache → no request.
    bars2 = hc.fetch_candle_range("AAA", "1h", grid, grid + 3 * STEP)
    assert bars2 == bars
    assert len(calls) == 1


def test_closed_bars_as_of_excludes_forming_bar(monkeypatch):
    calls = []
    grid = 1_700_000_000_000
    grid -= grid % STEP
    prices = {grid + i * STEP: 100.0 for i in range(0, 5)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    # as_of exactly at grid+3*STEP: bars opening at grid..grid+2h closed;
    # the grid+3h bar (closes at grid+4h) is still forming.
    bars = hc.closed_bars_as_of("AAA", "1h", grid, grid + 3 * STEP)
    assert [b.t for b in bars] == [grid, grid + STEP, grid + 2 * STEP]


def test_disk_cache_roundtrip(monkeypatch, tmp_path):
    calls = []
    grid = 1_700_000_000_000
    grid -= grid % STEP
    prices = {grid + i * STEP: 100.0 + i for i in range(3)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))
    hc.fetch_candle_range("AAA", "1h", grid, grid + 2 * STEP)
    assert hc.flush_disk_cache() is True

    # Fresh process state: disk reload, no API call.
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "candles.json"))
    calls.clear()
    bars = hc.fetch_candle_range("AAA", "1h", grid, grid + 2 * STEP)
    assert len(bars) == 3
    assert bars[1].c == 101.0
    assert calls == []


def test_merge_spans():
    assert hc._merge_spans([(1, 5), (3, 9), (20, 25), (24, 30)]) == \
        [(1, 9), (20, 30)]
    assert hc._merge_spans([]) == []


def test_warm_cache_merges_per_coin(monkeypatch):
    calls = []
    grid = 1_700_000_000_000
    grid -= grid % STEP
    prices = {grid + i * STEP: 100.0 for i in range(-2, 200)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))
    recs = [
        {"coin": "AAA", "timestamp": grid},
        {"coin": "AAA", "timestamp": grid + STEP},   # overlaps → merged
        {"coin": "BBB", "timestamp": grid},
        {"coin": None, "timestamp": grid},            # skipped
        {"coin": "CCC"},                              # skipped
    ]
    stats = hc.warm_cache_from_records(recs)
    assert stats["records_used"] == 3
    assert stats["records_skipped"] == 2
    coins = {c["coin"] for c in calls}
    assert coins == {"AAA", "BBB"}
    # AAA's two overlapping spans merged into one range request.
    aaa = [c for c in calls if c["coin"] == "AAA"]
    assert len(aaa) == 1


def test_bad_interval_rejected():
    with pytest.raises(ValueError):
        hc.fetch_candle_range("AAA", "7h", 0, 10 * STEP)
