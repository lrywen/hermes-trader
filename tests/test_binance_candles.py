"""Offline tests for the Binance klines replay data source (no network)."""
from __future__ import annotations

import pytest

from hermes_trader.data import binance_candles as bc


def test_symbol_mapping():
    assert bc.to_binance_symbol("BTC") == "BTCUSDT"
    assert bc.to_binance_symbol("kPEPE") is None      # HIP-3 1000x scale
    assert bc.to_binance_symbol("abc:xyz") is None
    assert bc.interval_ms("5m") == 300_000


def test_parse_ohlcv_and_quote():
    # [ot, o, h, l, c, baseVol, ct, quoteVol, ...]
    rows = [
        [0, "100", "101", "99", "100.5", "10", 1, "1005"],
        [300000, "100.5", "102", "100", "101", "5", 2, "505"],
        ["bad"],
        [600000, "1", "1", "2", "1", "1", 3, "1"],  # high<low -> dropped
    ]
    bars, quote = bc._parse(rows)
    assert sorted(bars) == [0, 300000]
    assert bars[0].c == 100.5
    assert quote[0] == 1005.0 and quote[300000] == 505.0


def test_fetch_pages_forward_and_caches(monkeypatch):
    bc.reset_cache()
    calls = []

    def fake_get(url, timeout=20):
        calls.append(url)
        # 1000 rows per page; emit a deterministic grid based on startTime
        import re
        st = int(re.search(r"startTime=(\d+)", url).group(1))
        iv = 300_000
        if len(calls) == 1:
            return [[st + i * iv, 1, 1, 1, 1, 1, 0, 1] for i in range(1000)]
        # second page: one more partial page then empty
        if len(calls) == 2:
            return [[st + i * iv, 1, 1, 1, 1, 1, 0, 1] for i in range(50)]
        return []

    monkeypatch.setattr(bc, "_http_get_json", fake_get)
    iv = 300_000
    end = (1000 + 49) * iv
    rows = bc.fetch_candle_range("BTC", "5m", 0, end, use_cache=False)
    assert len(rows) == 1050
    assert rows[0].t == 0 and rows[-1].t == end
    assert len(calls) >= 2


def test_fetch_over_ceiling_rejected():
    iv = 300_000
    with pytest.raises(ValueError):
        bc.fetch_candle_range("BTC", "5m", 0,
                              (bc.MAX_TOTAL_BARS + 10) * iv, use_cache=False)
