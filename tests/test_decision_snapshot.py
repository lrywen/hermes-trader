"""P1-b decision snapshot ledger tests."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from hermes_trader.agents.decision_snapshot import (
    append_snapshots,
    build_snapshot_record,
    record_cycle_snapshots,
)


def _perc(coin="BTC", **kw):
    base = {
        "coin": coin,
        "bar_close_ms": 1790000000000,
        "mid": 81800.0,
        "composite_score": 61.5,
        "triggers": [
            {"name": "momentumBurst", "fired": True, "strength": 0.8},
            {"name": "slowBurn", "fired": False, "strength": 0.1},
        ],
        "volume_profile": {"vpoc": 81000},
        "cvd_divergence": {"bullish": True},
        "whale_signal": None,
    }
    base.update(kw)
    return base


def test_build_snapshot_bounded_fields():
    rec = build_snapshot_record(
        _perc(), cycle_ts_ms=1790000300000, equity=30.59,
        regime="range", mode="SHADOW",
    )
    assert rec["coin"] == "BTC"
    assert rec["mid"] == 81800.0
    assert rec["composite_score"] == 61.5
    assert rec["equity"] == 30.59
    assert rec["regime"] == "range"
    assert rec["mode"] == "SHADOW"
    assert rec["bar_close_ms"] == 1790000000000
    assert rec["ts"] == 1790000300000
    # only fired/name/strength carried, trigger dict is compact
    assert rec["triggers"][0] == {"name": "momentumBurst", "fired": True,
                                  "strength": 0.8}
    # whale None is omitted; volume/cvd kept
    assert "whale_signal" not in rec
    assert rec["volume_profile"] == {"vpoc": 81000}


def test_build_snapshot_requires_coin():
    assert build_snapshot_record({"mid": 1}, cycle_ts_ms=1) is None


def test_build_snapshot_drops_non_finite_mid():
    rec = build_snapshot_record(_perc(mid=float("nan")), cycle_ts_ms=1)
    assert rec["mid"] is None
    rec2 = build_snapshot_record(_perc(mid="junk"), cycle_ts_ms=1)
    assert rec2["mid"] is None


def test_append_day_partitioned_jsonl_is_append_only(tmp_path):
    ts = int(datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    n1 = append_snapshots(
        [build_snapshot_record(_perc("BTC"), cycle_ts_ms=ts, equity=30.0)],
        root=str(tmp_path), ts_ms=ts,
    )
    n2 = append_snapshots(
        [build_snapshot_record(_perc("ETH"), cycle_ts_ms=ts, equity=31.0)],
        root=str(tmp_path), ts_ms=ts,
    )
    assert (n1, n2) == (1, 1)
    f = tmp_path / "date=2026-10-14" / "snapshots.jsonl"
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # append-only, second group did not overwrite
    coins = [json.loads(x)["coin"] for x in lines]
    assert coins == ["BTC", "ETH"]


def test_append_skips_nan_and_unidentifiable(tmp_path):
    ts = 1790000000000
    # A record with a NaN somewhere must not produce illegal JSON; append
    # skips it rather than writing a bare NaN token.
    bad = {"coin": "BTC", "mid": float("nan")}
    good = {"coin": "ETH", "mid": 1.0}
    n = append_snapshots([bad, good, {"mid": 1.0}], root=str(tmp_path), ts_ms=ts)
    assert n == 1
    files = list(tmp_path.glob("date=*/snapshots.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["coin"] == "ETH"


def test_record_cycle_helper(tmp_path):
    n = record_cycle_snapshots(
        [_perc("BTC"), _perc("ETH"), {"no_coin": True}],
        equity=30.59, mode="SHADOW", root=str(tmp_path),
        cycle_ts_ms=1790000000000,
    )
    assert n == 2  # the coin-less perception is dropped


def test_append_bad_root_returns_zero_does_not_raise():
    # An unwritable root degrades to 0 written (best-effort, never blocks loop).
    n = append_snapshots([{"coin": "BTC", "mid": 1.0}],
                         root="/nonexistent-dir-xyz-cannot-create/sub",
                         ts_ms=1790000000000)
    assert n == 0
