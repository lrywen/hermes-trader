#!/usr/bin/env python3
"""Reconcile shadow veto-gate records with forward returns (R1 + P0-2).

Every block/veto gate that runs in shadow writes a probe row but only a couple
of them ever had an outcome backfiller, so the 00:45 grader saw "backfill=0" for
the rest and their counterfactual ("what would the blocked trade have done?")
was never measurable. This script stamps a cost-aware forward outcome onto each
*veto-hit* row so a gate can be judged on real data instead of intuition.

One unified geometry per arm (default: entry at the signal bar, exit 72h later,
net of a round-trip taker cost, direction taken from the record's ``side`` so a
refused short is not graded as a long), with per-arm overrides where the owner
arm has a different horizon:

  * trend_filter_200ma    block arm, ``trend_would_block``; entry = record price.
  * per_coin_cooldown     block arm, ``would_block``; entry = signal-bar close.
  * regime_direction      block arm, ``would_block`` (counter-trend signal).
  * daily_extension_cap   anti-chase, ``ext_would_block``.
  * runner_gate           ``admitted is False`` (gate refused the candidate);
                          entry = ``mid_price`` when present.
  * confidence_decay      ``would_block_gate``; entry = ``cf_entry_px``,
                          24h horizon (mirrors its cf_hold_bars owner geometry).

Gates NOT handled here because a dedicated reconciler already owns the same
file with its own geometry (grading them twice would mix horizons): pullback,
ta_late_entry, xs_reversal, sizing_v2 / atr_regime_calib (change arms), and
risk_tuning rules (per_coin_cooldown relax / deep_slow_burn_veto). ``market_
circuit`` is a portfolio-wide halt with no coin to grade.

Cost-aware: a round-trip taker cost is subtracted before win/loss so a
marginally-positive move isn't counted as a win. Read-only on the market
(1h candles, disk cache first), never places an order.

Usage:
    python3 scripts/reconcile_gate_outcomes.py                 # dry-run
    python3 scripts/reconcile_gate_outcomes.py --write
"""
from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import sys
import time
from typing import Any, Optional

FWD_HOURS = 72
_STEP = 3600_000
# Import-time "now": any forward bar closing after this is immature, so the
# record must be left alone rather than probed for candles it cannot have.
_NOW_MS = int(time.time() * 1000)
# Round-trip taker cost fraction used only for grading direction (HL taker).
ROUND_TRIP_COST_PCT = 0.0009

_ARMS = (
    {
        "name": "trend_filter_200ma",
        "file": "trend_filter_shadow.jsonl",
        "hit": "trend_would_block",
        "ts_field": "timestamp",
        "ts_ms": False,
        "entry": "price",
    },
    {
        "name": "per_coin_cooldown",
        "file": "per_coin_cooldown_shadow.jsonl",
        "hit": "would_block",
        "ts_field": "ts",
        "ts_ms": True,
        "entry": None,
    },
    {
        "name": "daily_extension_cap",
        "file": "daily_extension_cap_shadow.jsonl",
        "hit": "ext_would_block",
        "ts_field": "timestamp",
        "ts_ms": False,
        "entry": None,
    },
    {
        "name": "regime_direction",
        "file": "regime_direction_shadow.jsonl",
        "hit": "would_block",
        "ts_field": "ts",
        "ts_ms": True,
        "entry": None,
    },
    {
        "name": "runner_gate",
        "file": "runner_gate_shadow.jsonl",
        "hit": "admitted",
        "hit_value": False,  # gate REFUSED the candidate
        "ts_field": "ts",
        "ts_ms": True,
        "entry": "mid_price",
    },
    {
        "name": "confidence_decay",
        "file": "confidence_decay_shadow.jsonl",
        "hit": "would_block_gate",
        "ts_field": "ts",
        "ts_ms": True,
        "entry": "cf_entry_px",
        "fwd_hours": 24,  # owner cf geometry is a 24x1h hold
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
    # ISO "YYYY-MM-DDTHH:MM:SSZ" — UTC, so calendar.timegm (not mktime, which
    # would read the struct as container-local time).
    try:
        return calendar.timegm(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")) * 1000
    except (TypeError, ValueError):
        return None


def _bars(coin: str, start_ms: int, horizon: int) -> dict[int, float]:
    """1h closes keyed by bar-start ms, spanning grid0 .. grid0+horizon.

    Prefers the on-disk candle cache (P0-1 backfilled it to today) so a daily
    reconcile over 8 arms doesn't hammer /info; falls back to the live API when
    the coin/range is not cached.

    The window is clipped to "now": an un-clipped end in the future never
    becomes cache-resident, so every record would re-issue its own candle
    request (runner_gate alone has ~2k such records => a 10-minute hang).
    """
    grid0 = start_ms - (start_ms % _STEP)
    end_ms = min(grid0 + (horizon + 3) * _STEP, _NOW_MS)
    try:
        from hermes_trader.data import historical_candles as hc

        cached = hc.closed_bars_as_of(str(coin), "1h", grid0, end_ms,
                                      use_cache=True)
        if cached:
            return {b.t: float(b.c) for b in cached}
    except Exception:
        pass

    from hermes_trader.client.hl_client import _http_post

    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": grid0,
        "endTime": end_ms}}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list):
        return {}
    return {int(c["t"]): float(c["c"]) for c in raw}


def _grade(rec: dict[str, Any], arm: dict[str, Any]) -> bool:
    """Return True if this record was newly graded (rec mutated in place)."""
    if rec.get("outcome") in ("win", "loss"):
        return False
    hit_value = arm.get("hit_value", True)
    if rec.get(arm["hit"]) != hit_value:
        return False
    coin = rec.get("coin")
    start_ms = _record_ms(rec, arm)
    if not coin or start_ms is None:
        return False

    fwd_hours = int(arm.get("fwd_hours", FWD_HOURS))
    grid0 = start_ms - (start_ms % _STEP)
    exit_ms = grid0 + fwd_hours * _STEP
    if exit_ms + _STEP > _NOW_MS:
        return False  # exit bar has not closed yet -> nothing to grade
    bars = _bars(coin, start_ms, fwd_hours)
    exit_px = bars.get(exit_ms)
    if exit_px is None:
        return False  # data gap for this coin/window

    entry_px = rec.get(arm["entry"]) if arm["entry"] else None
    try:
        entry_px = float(entry_px)
    except (TypeError, ValueError):
        entry_px = 0.0
    if entry_px <= 0:
        entry_px = bars.get(grid0)
    try:
        entry_px = float(entry_px)
    except (TypeError, ValueError):
        return False
    if entry_px <= 0:
        return False

    gross_pct = (exit_px / entry_px - 1.0) * 100.0
    # Side-aware: a refused SHORT profits when price falls, so grading it as a
    # long would invert its sign (runner_gate is ~45% shorts).
    if str(rec.get("side") or "long").lower() == "short":
        gross_pct = -gross_pct
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


def _warm(arm: dict[str, Any], recs: list[dict[str, Any]]) -> None:
    """Prefetch the forward windows of every ungraded hit in ONE merged pass.

    Without this the grader issues one candle request per record; signal_age_
    decay has ~100k scan rows, so a per-record fetch never finishes inside the
    daily cron window. ``warm_cache_from_records`` merges each coin's spans, so
    the cost collapses to a handful of requests per coin.
    """
    horizon = int(arm.get("fwd_hours", FWD_HOURS))
    points = []
    for rec in recs:
        ts = _record_ms(rec, arm)
        if not rec.get("coin") or not ts:
            continue
        # Immature exits have no bar to fetch -> don't warm them either.
        if ts - (ts % _STEP) + (horizon + 1) * _STEP > _NOW_MS:
            continue
        points.append({"coin": rec["coin"], "ts": ts})
    if not points:
        return
    try:
        from hermes_trader.data import historical_candles as hc

        hc.warm_cache_from_records(points, interval="1h", before_bars=1,
                                   after_bars_max=horizon + 8)
    except Exception:
        pass  # best-effort: _bars still falls back to a direct fetch


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
        hit_value = arm.get("hit_value", True)
        pending = [r for r in recs
                   if r.get(arm["hit"]) == hit_value
                   and r.get("outcome") not in ("win", "loss")]
        _warm(arm, pending)
        changed = 0
        for rec in recs:
            stats["records"] += 1
            if rec.get(arm["hit"]) == hit_value:
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

    print(f"=== gate outcome reconciliation ({len(_ARMS)} shadow veto arms) ===")
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
