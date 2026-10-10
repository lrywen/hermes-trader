#!/usr/bin/env python3
"""Reconcile signal_age_decay / trend_filter_200ma shadow records with forward
returns (R1: close the outcome-backfill gap).

Both arms are scored by ``shadow_grade`` using ``outcome`` (win/loss) and
``pnl_usd``, and both were stuck with backfill=0 — signal_age_decay had no
reconciler at all and trend_filter's historical backfiller wrote an isolated
file, never the live JSONL. This script fixes both by stamping the LIVE file
(and its size-rotated siblings):

  * signal_age_decay (change arm): grades records where ``would_block`` is True.
    The record carries no price, so entry = close of the 1h bar covering the
    signal timestamp; exit = close 72h later.
  * trend_filter_200ma (block arm): grades records where ``trend_would_block``
    is True; entry = the record's own ``price``, exit = close 72h later.

Cost-aware: a round-trip taker cost is subtracted before win/loss so a
marginally-positive move isn't counted as a win. Read-only on the market
(1h candleSnapshot), never places an order.

Usage:
    python3 scripts/reconcile_gate_outcomes.py                 # dry-run
    python3 scripts/reconcile_gate_outcomes.py --write
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Any, Optional

FWD_HOURS = 72
_STEP = 3600_000
# Round-trip taker cost fraction used only for grading direction (HL taker).
ROUND_TRIP_COST_PCT = 0.0009

_ARMS = (
    {
        "name": "signal_age_decay",
        "file": "signal_age_decay_shadow.jsonl",
        "hit": "would_block",
        "ts_field": "ts",
        "ts_ms": True,
        "entry": None,  # resolve entry from candles
    },
    {
        "name": "trend_filter_200ma",
        "file": "trend_filter_shadow.jsonl",
        "hit": "trend_would_block",
        "ts_field": "timestamp",
        "ts_ms": False,
        "entry": "price",
    },
)


def _data_dir() -> str:
    return os.environ.get("HERMES_DATA_DIR", "/data")


def _record_ms(rec: dict[str, Any], arm: dict[str, Any]) -> Optional[int]:
    raw = rec.get(arm["ts_field"])
    if raw is None:
        return None
    if arm["ts_ms"]:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    # ISO "YYYY-MM-DDTHH:MM:SSZ"
    try:
        return int(time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))) * 1000
    except (TypeError, ValueError):
        return None


def _bars(coin: str, start_ms: int, horizon: int) -> dict[int, float]:
    """1h closes keyed by bar-start ms, spanning grid0 .. grid0+horizon."""
    from hermes_trader.client.hl_client import _http_post

    grid0 = start_ms - (start_ms % _STEP)
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": grid0,
        "endTime": grid0 + (horizon + 3) * _STEP}}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list):
        return {}
    return {int(c["t"]): float(c["c"]) for c in raw}


def _grade(rec: dict[str, Any], arm: dict[str, Any]) -> bool:
    """Return True if this record was newly graded (rec mutated in place)."""
    if rec.get("outcome") in ("win", "loss"):
        return False
    if rec.get(arm["hit"]) is not True:
        return False
    coin = rec.get("coin")
    start_ms = _record_ms(rec, arm)
    if not coin or start_ms is None:
        return False

    bars = _bars(coin, start_ms, FWD_HOURS)
    grid0 = start_ms - (start_ms % _STEP)
    exit_px = bars.get(grid0 + FWD_HOURS * _STEP)
    if exit_px is None:
        return False  # forward bar not printed yet / data gap

    if arm["entry"]:
        entry_px = rec.get(arm["entry"])
    else:
        entry_px = bars.get(grid0)
    try:
        entry_px = float(entry_px)
    except (TypeError, ValueError):
        return False
    if entry_px <= 0:
        return False

    gross_pct = (exit_px / entry_px - 1.0) * 100.0
    net_pct = gross_pct - ROUND_TRIP_COST_PCT * 100.0
    notional = rec.get("trade_notional_usd")
    pnl_usd = round(net_pct / 100.0 * float(notional), 4) if notional else None

    rec["entry_px"] = round(entry_px, 8)
    rec["exit_px"] = round(exit_px, 8)
    rec["pnl_pct"] = round(net_pct, 4)
    if pnl_usd is not None:
        rec["pnl_usd"] = pnl_usd
    rec["outcome"] = "win" if net_pct > 0 else "loss"
    return True


def _iter_files(base_file: str) -> list[str]:
    paths = sorted(glob.glob(base_file + ".*"),
                   key=lambda p: len(p))  # .1 before .2 ...
    return [base_file] + paths


def process_arm(arm: dict[str, Any], *, write: bool) -> dict[str, int]:
    stats = {"records": 0, "eligible": 0, "newly_graded": 0}
    base_file = os.path.join(_data_dir(), arm["file"])
    for path in _iter_files(base_file):
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            rows = [line for line in f if line.strip()]
        recs = []
        for line in rows:
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
        changed = 0
        for rec in recs:
            stats["records"] += 1
            if rec.get(arm["hit"]) is True:
                stats["eligible"] += 1
            if _grade(rec, arm):
                changed += 1
        stats["newly_graded"] += changed
        if write and changed:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in recs:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    print("=== gate outcome reconciliation (signal_age_decay / trend_filter) ===")
    for arm in _ARMS:
        stats = process_arm(arm, write=args.write)
        print(f"  {arm['name']:20s} records={stats['records']} "
              f"eligible(hit)={stats['eligible']} "
              f"newly_graded={stats['newly_graded']}")
    if not args.write:
        print("(dry-run; pass --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
