"""R3: permanent tick tape for BTC/ETH via the Hyperliquid public trades feed.

Every trade is appended with its exchange timestamp and the contemporaneous
best bid/ask spread, so a true CVD and a spread/impact profile can be derived
offline. These tests pin the tape-row shape, the spread computation, and the
dispatch gate.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from hermes_trader.client import ws_client


class _FakeInfo:
    def __init__(self):
        self.subs = []

    def subscribe(self, sub, cb):
        self.subs.append(sub)
        return 1


def _ws(tmp_path, monkeypatch):
    ws = object.__new__(ws_client.HyperliquidWebSocket)
    ws._info = _FakeInfo()
    ws._trades_coins = set()
    ws._trades_capture_enabled = False
    ws._tape_coins = set()
    ws._tape_capture_enabled = False
    ws._book_coins = set()
    ws._latest_book = {}
    import threading
    ws._book_lock = threading.Lock()
    monkeypatch.setattr(ws_client, "_TAPE_RAW_DIR", str(tmp_path / "tape-raw"))
    return ws


def test_start_tape_capture_defaults_to_btc_eth(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    n = ws.start_tape_capture()
    assert n == 2
    assert ws._tape_coins == {"BTC", "ETH"}
    # trades dispatch filters on _trades_coins, so tape coins must be there
    assert {"BTC", "ETH"} <= ws._trades_coins
    # book feed is also requested to populate the spread
    assert {"BTC", "ETH"} <= ws._book_coins
    types = {s["type"] for s in ws._info.subs}
    assert types == {"trades", "l2Book"}


def test_tape_row_written_with_spread(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ws.start_tape_capture()
    ws._latest_book["BTC"] = {
        "b": [["100.0", "2"]], "a": [["100.1", "3"]]}
    now_ms = int(
        datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp() * 1000)
    trade = {"coin": "BTC", "side": "B", "px": "100.05", "sz": "1.5",
             "time": now_ms, "hash": "abc"}
    ws._on_trades({"data": [trade]})

    day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / "tape-raw" / f"date={day}" / "BTC.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["coin"] == "BTC" and r["side"] == "B"
    assert r["px"] == 100.05 and r["sz"] == 1.5
    assert r["tt"] == now_ms
    assert r["bid"] == 100.0 and r["ask"] == 100.1
    assert abs(r["spread"] - 0.1) < 1e-9
    assert r["spread_bps"] > 0
    assert r["hash"] == "abc"


def test_spread_none_when_book_cold(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ws.start_tape_capture()
    trade = {"coin": "ETH", "side": "A", "px": "200", "sz": "1",
             "time": 0}
    ws._on_trades({"data": [trade]})
    path = tmp_path / "tape-raw" / "date=1970-01-01" / "ETH.jsonl"
    r = json.loads(path.read_text().splitlines()[0])
    assert r["bid"] is None and r["ask"] is None
    assert r["spread"] is None and r["spread_bps"] is None


def test_no_tape_write_when_disabled(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ws._trades_coins = {"BTC"}
    ws._tape_coins = {"BTC"}
    # capture not enabled
    ws._on_trades({"data": [
        {"coin": "BTC", "side": "B", "px": "1", "sz": "1", "time": 0}]})
    assert not (tmp_path / "tape-raw").exists()


def test_non_tape_coin_not_recorded(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ws.start_tape_capture()
    ws._trades_coins.add("SOL")
    ws._on_trades({"data": [
        {"coin": "SOL", "side": "B", "px": "10", "sz": "1", "time": 0}]})
    assert not (tmp_path / "tape-raw" / "date=1970-01-01" / "SOL.jsonl").exists()


def test_buy_and_sell_trades_both_recorded(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ws.start_tape_capture()
    ws._on_trades({"data": [
        {"coin": "BTC", "side": "B", "px": "100", "sz": "1", "time": 0},
        {"coin": "BTC", "side": "A", "px": "100", "sz": "1", "time": 0},
    ]})
    path = tmp_path / "tape-raw" / "date=1970-01-01" / "BTC.jsonl"
    sides = [json.loads(l)["side"] for l in path.read_text().splitlines()]
    assert sides == ["B", "A"]
