"""Tests for ch_real_fill_monitor: labeled open->close join and bucketing."""
from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")


def _load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mon = _load("ch_real_fill_monitor", "ch_real_fill_monitor.py")


def _open(pid, coin, fired, comp, side="long", ts=1, account="taker"):
    return {"type": "open", "id": "o" + pid, "position_id": pid, "ts": ts,
            "coin": coin, "side": side, "price": 100.0,
            "fired_names": fired, "composite_score": comp}


def _close(pid, roe, reason, ts=2, account="taker"):
    return {"type": "close", "id": "c" + pid, "position_id": pid, "ts": ts,
            "coin": "X", "side": "long", "price": 101.0,
            "realized_pnl_pct": roe, "reason": reason,
            "hold_minutes": 10.0}


def _book(fills, account="taker"):
    return {"version": 2, "accounts": {account: {"fills": fills}}}


def test_closed_labeled_join_matches_position_id():
    book = _book([
        _open("p1", "AAA", ["dailyMover", "uptrendMomentum"], 32.0),
        _close("p1", 7.1, "ignite_timeout (90min no launch)"),
        _open("p2", "BBB", ["momentumBurst"], 40.0),  # still open, censored
        {"type": "open", "id": "o3", "position_id": "p3", "ts": 3,
         "coin": "CCC", "side": "long", "price": 1.0},  # legacy unlabeled
        _close("p3", -5.0, "max_loss"),
    ])
    rows = mon.closed_labeled_round_trips(book)
    assert len(rows) == 1
    r = rows[0]
    assert r["position_id"] == "p1"
    # mover beats trend precedence? production precedence is trend BEFORE mover
    assert r["channel"] == "trend"
    assert r["roe_pct"] == 7.1
    assert r["win"] is True
    assert mon.reason_family(r["reason"]) == "ignite_timeout"


def test_unlabeled_and_open_are_censored_not_bucketed():
    book = _book([
        _open("p1", "XXX", ["momentumBurst"], 40.0),  # open only, censored
        {"type": "open", "position_id": "leg", "ts": 0, "coin": "L",
         "side": "long", "price": 1.0},
        _close("leg", -9.0, "max_loss"),
    ])
    assert mon.closed_labeled_round_trips(book) == []
    cen = mon.census(book)["taker"]
    assert cen["labeled_opens"] == 1
    assert cen["closed_labeled"] == 0
    assert cen["open_labeled_censored"] == 1


def test_gate_channel_when_composite_passes_and_no_bypass_name():
    book = _book([
        _open("g1", "GGG", ["rangeCompression"], 55.2),
        _close("g1", 1.0, "floor_breach (2x consec)"),
    ])
    rows = mon.closed_labeled_round_trips(book)
    assert rows[0]["channel"] == "gate"
    assert rows[0]["band"] == ">=54"
    assert mon.reason_family(rows[0]["reason"]) == "floor_breach"


def test_mover_channel_isolates_daily_mover_without_trend_name():
    # dailyMover alone (no uptrend/downtrendMomentum) -> mover
    book = _book([_open("m1", "MMM", ["dailyMover"], 30.0),
                  _close("m1", -3.0, "stale_flat_timeout")])
    rows = mon.closed_labeled_round_trips(book)
    assert rows[0]["channel"] == "mover"
    assert mon.reason_family(rows[0]["reason"]) == "stale_flat_timeout"


def test_stats_and_bucket_aggregation():
    rows = [
        {"channel": "mover", "roe_pct": -4.0},
        {"channel": "mover", "roe_pct": 2.0},
        {"channel": "mover", "roe_pct": 2.0},
        {"channel": "gate", "roe_pct": 5.0},
    ]
    b = mon.bucket(rows, "channel")
    assert b["mover"]["n"] == 3
    assert b["mover"]["median"] == 2.0
    assert round(b["mover"]["mean"], 3) == 0.0
    assert b["mover"]["win_pct"] == round(200.0 / 3, 1)
    assert b["gate"]["n"] == 1


def test_accounts_kept_separate():
    book = {"accounts": {
        "taker": {"fills": [_open("p1", "A", ["dailyMover"], 30.0),
                            _close("p1", -1.0, "max_loss")]},
        "maker_shadow": {"fills": [_open("p1", "A", ["dailyMover"], 30.0),
                                   _close("p1", 0.5, "floor_breach")]},
    }}
    rows = mon.closed_labeled_round_trips(book)
    assert len(rows) == 2
    assert {r["account"] for r in rows} == {"taker", "maker_shadow"}
    cen = mon.census(book)
    assert cen["taker"]["closed_labeled"] == 1
    assert cen["maker_shadow"]["closed_labeled"] == 1
