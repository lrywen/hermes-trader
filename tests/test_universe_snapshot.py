"""M1B: point-in-time universe snapshot — record / PIT query / scan hook / backtest flag.

Covers the survivorship + look-ahead fix: snapshots are append-only and
`snapshot_as_of` returns the most recent version visible at a given time, so
backtests never select coins using today's universe/volume.
"""
from __future__ import annotations

import json
import time

from hermes_trader.data import universe_snapshot as us


def _market(coin, vol, funding="0.0001"):
    return {"coin": coin, "type": "perp", "dex": None, "dayNtlVlm": vol,
            "openInterest": "1", "funding": funding, "markPx": "1.0"}


def test_record_and_snapshot_as_of_pit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    t0 = time.time() * 1000.0
    # Snapshot at t0 with BTC+ETH; later snapshot at t0+1h adds SOL.
    us.record_universe_snapshot([_market("BTC", 1e9), _market("ETH", 5e8)],
                                ts_ms=t0, min_interval_s=0)
    us.record_universe_snapshot([_market("BTC", 1.2e9), _market("ETH", 6e8),
                                 _market("SOL", 2e8)], ts_ms=t0 + 3600_000, min_interval_s=0)
    # As-of between the two snapshots must see only the first (no SOL).
    mid = us.snapshot_as_of(t0 + 1800_000)
    coins = {m["coin"] for m in mid["markets"]}
    assert coins == {"BTC", "ETH"}
    # As-of after the second snapshot sees SOL.
    later = us.snapshot_as_of(t0 + 7200_000)
    assert {m["coin"] for m in later["markets"]} == {"BTC", "ETH", "SOL"}
    # Before any snapshot → None.
    assert us.snapshot_as_of(t0 - 86_400_000) is None


def test_record_min_interval_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    t0 = time.time() * 1000.0
    p1 = us.record_universe_snapshot([_market("BTC", 1e9)], ts_ms=t0, min_interval_s=300)
    assert p1 is not None
    # 60s later, within the 300s gap → skipped (returns None, no new row).
    p2 = us.record_universe_snapshot([_market("BTC", 2e9)], ts_ms=t0 + 60_000, min_interval_s=300)
    assert p2 is None
    rows = [json.loads(l) for l in open(p1) if l.strip()]
    assert len(rows) == 1


def test_record_swallows_errors(tmp_path, monkeypatch):
    # A non-serializable market must not raise — best-effort, returns None.
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    bad = [{"coin": "BTC", "type": "perp", "dayNtlVlm": object()}]
    # _compact_market only picks known keys; force a failure via a bad ts.
    assert us.record_universe_snapshot(bad, ts_ms="not-a-number") is None


def test_scan_hook_records_snapshot(tmp_path, monkeypatch):
    """scan_once must snapshot the universe on BOTH resolution paths.

    Regression: the live loop always passes a pre-fetched universe
    (trading_loop: scan_once(universe=universe)), so a hook placed only inside
    the `universe is None` branch never fired in production and no PIT snapshot
    was ever written. Drive scan_once itself with a pre-fetched universe.
    """
    from hermes_trader.agents import perception
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(perception, "record_universe_snapshot",
                        lambda markets, **kw: calls.append(len(markets)))
    # No mids / no candle fetches: the scan returns early on an empty market
    # list, so this stays offline and only exercises the universe step.
    monkeypatch.setattr(perception, "fetch_all_mids", lambda **kw: {})
    monkeypatch.setattr(perception, "get_universe",
                        lambda **kw: [_market("BTC", 1e9), _market("ETH", 5e8)])

    # Path A: caller supplies the universe (production path).
    perception.scan_once(universe=[_market("BTC", 1e9), _market("ETH", 5e8)],
                        min_score=0)
    assert calls == [2]

    # Path B: no universe supplied → resolved via get_universe.
    perception.scan_once(min_score=0)
    assert calls == [2, 2]

    # The production call site really does pass a pre-fetched universe.
    from pathlib import Path
    loop_src = (Path(__file__).resolve().parents[1] / "scripts" /
                "trading_loop.py").read_text(encoding="utf-8")
    assert "universe=universe, min_score=min_score" in loop_src


def test_backtest_pit_universe_selection(tmp_path, monkeypatch):
    """--universe pit picks coins from the snapshot as of start, not today."""
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    # Snapshot 30 days ago: only BTC + ETH (SOL "not yet listed").
    old_ts = time.time() * 1000.0 - 30 * 86_400_000.0
    us.record_universe_snapshot([_market("BTC", 1e9), _market("ETH", 5e8)],
                                ts_ms=old_ts, min_interval_s=0)
    snap = us.snapshot_as_of(old_ts + 86_400_000.0)
    assert snap is not None
    perps = [m for m in snap["markets"] if m["type"] == "perp"]
    coins = sorted(perps, key=lambda m: m["dayNtlVlm"], reverse=True)[:20]
    assert [c["coin"] for c in coins] == ["BTC", "ETH"]  # no SOL (look-ahead avoided)
