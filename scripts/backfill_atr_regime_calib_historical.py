#!/usr/bin/env python3
"""Historical backfiller for the atr_regime_calib shadow arm (second pilot).

Speeds up arm grading by replacing the *wait* for forward bars to print in
real time with a point-in-time replay over immutable closed 1h history
(audited 2026-09-14: pinned historical ranges and live recent-window fetches
return byte-identical OHLC for closed bars).

Scope / safety contract (pilot, atr_regime_calib ONLY):
  * dry-run by default; ``--write`` writes an ISOLATED product
    (``atr_regime_calib_shadow.backfill.jsonl``), never the live JSONL — use
    ``--out`` to redirect;
  * only records with ``outcome`` null/absent are graded (idempotent); the
    live file and its .1..5 rotations are read-only inputs here;
  * every produced row is tagged ``outcome_source="historical_replay"`` plus
    ``entry_px_source`` and ``fee_bps`` so the grader can stratify;
  * point-in-time: only bars CLOSED at the grading instant are used, via
    hermes_trader.data.historical_candles.closed_bars_as_of;
  * geometry preserved from reconcile_change_arms_shadow.reconcile_arm
    (atr branch): entry fills at the open of the first 1h bar to open
    *strictly after* the signal; the trade walks forward HOLD_BARS bars,
    exiting early if a stop at the arm's width is touched (bar low), else at
    the window close. v1 (raw_stop_pct) and v2 (calibrated_stop_pct) are
    simulated from the same entry; edge = v1 net pct - v2 net pct, and
    outcome "win" marks edge > 0 (the calibration forgoes return => arm
    hurts), mirroring the live counterfactual convention.
  * fee note: the round-trip 5bps is charged on BOTH legs, so it cancels in
    the v1-minus-v2 edge (edge_net == edge_gross); unlike xs_reversal there
    is no separate gross verdict to carry — only the live net convention.

Pure paper tooling: sets HERMES_BACKTEST=1, never orders, never writes live
state apart from the regenerable candle cache.

Usage:
    python3 scripts/backfill_atr_regime_calib_historical.py            # dry-run
    python3 scripts/backfill_atr_regime_calib_historical.py --write
    python3 scripts/backfill_atr_regime_calib_historical.py \
        --file /data/atr_regime_calib_shadow.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.data import historical_candles as hc  # noqa: E402

DEFAULT_FILE = "/data/atr_regime_calib_shadow.jsonl"
DEFAULT_OUT = "/data/atr_regime_calib_shadow.backfill.jsonl"
FILE = os.environ.get("HERMES_ATR_REGIME_CALIB_SHADOW_FILE", DEFAULT_FILE)

# Pilot whitelist — this script is arm-specific by construction.
ARM = "atr_regime_calib"
INTERVAL = "1h"
HOLD_BARS = 24          # mirrors reconcile_change_arms_shadow.HOLD_BARS
ROUND_TRIP_FEE_BPS = 5.0

MATURE_OUTCOMES = ("win", "loss")
_TERMINAL = MATURE_OUTCOMES + (
    "no_coin", "bad_timestamp", "not_material", "no_future_bars")


def signal_ms(rec: dict[str, Any]) -> Optional[int]:
    ts = rec.get("ts", rec.get("timestamp"))
    if isinstance(ts, (int, float)):
        v = int(ts)
        return v if v > 10_000_000_000 else v * 1000
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _iter_input_files(primary: str) -> list[str]:
    """Live primary plus size rotations, newest rotation first, primary last."""
    files = sorted(glob.glob(primary + ".*"), reverse=True)
    if os.path.exists(primary):
        files.append(primary)
    seen, out = set(), []
    for f in files:
        if os.path.abspath(f) not in seen:
            seen.add(os.path.abspath(f))
            out.append(f)
    return out


def _identity(r: dict[str, Any]) -> tuple[Any, ...]:
    # atr_calib records carry no entry_px; the stop widths pin the record.
    return (r.get("coin"), r.get("ts"), r.get("raw_stop_pct"),
            r.get("calibrated_stop_pct"))


def _load_inputs(primary: str) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    seen_ids: set[tuple[Any, ...]] = set()
    for path in _iter_input_files(primary):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = _identity(r)
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                r["_origin_file"] = os.path.basename(path)
                recs.append(r)
    return recs


def _simulate(side: str, entry_px: float, stop_pct: float, entry_idx: int,
              bars: list, hold_bars: int, fee_pct: float):
    """Walk forward from entry_idx; exit at stop (arm width) or window close.

    Mirrors reconcile_change_arms_shadow._simulate exactly (long-only stop on
    the bar low; a short stops above entry). Returns (exit_px, reason,
    net_pct) with pct in percent.
    """
    sign = 1.0 if side == "long" else -1.0
    exit_idx = min(entry_idx + hold_bars, len(bars) - 1)
    stop_frac = max(0.0, stop_pct) / 100.0
    for j in range(entry_idx, exit_idx + 1):
        bar = bars[j]
        if side == "long":
            adverse = (entry_px - bar.l) / entry_px
        else:
            adverse = (bar.h - entry_px) / entry_px
        if stop_frac > 0 and adverse >= stop_frac:
            exit_px = entry_px * (1.0 - sign * stop_frac)
            gross = sign * (exit_px - entry_px) / entry_px
            return exit_px, "stop", (gross - fee_pct) * 100.0
    exit_px = bars[exit_idx].c
    gross = sign * (exit_px - entry_px) / entry_px
    return exit_px, "window_close", (gross - fee_pct) * 100.0


def grade_record(rec: dict[str, Any], *, as_of_ms: Optional[int] = None,
                 use_cache: bool = True) -> str:
    """Grade one record with a pinned historical fetch; mutate rec in place.

    Returns the terminal/non-terminal outcome string. Mirrors
    reconcile_change_arms_shadow.reconcile_arm (atr branch) via the PIT data
    layer.
    """
    coin = rec.get("coin")
    t0 = signal_ms(rec)
    if not coin:
        return "no_coin"
    if not t0:
        return "bad_timestamp"
    # Material guard (live: _settle_atr_calib returns None -> not_material).
    if rec.get("would_change") is not True:
        rec["outcome"] = "not_material"
        return "not_material"
    v1_stop = float(rec.get("raw_stop_pct") or rec.get("core_stop_pct") or 0.0)
    v2_stop = float(rec.get("calibrated_stop_pct") or 0.0)

    as_of = as_of_ms if as_of_ms is not None else int(time.time() * 1000)
    step = hc.INTERVAL_MS[INTERVAL]
    grid0 = t0 - (t0 % step)
    # Entry bar is the first bar with open t strictly after t0 (== grid0+step
    # on a hole-free grid). The exit bar opens at entry+HOLD_BARS (offset
    # HOLD_BARS+1 from grid0) and closes one step later (offset HOLD_BARS+2).
    need_close = grid0 + (HOLD_BARS + 2) * step
    mature_cut = as_of >= need_close
    end_bound = min(as_of, need_close)
    bars = hc.closed_bars_as_of(str(coin), INTERVAL, grid0, end_bound,
                                use_cache=use_cache)

    entry_pos = None
    for i, b in enumerate(bars):
        if b.t > t0:
            entry_pos = i
            break
    if entry_pos is None or entry_pos + HOLD_BARS >= len(bars):
        # Window not fully closed yet -> retry later (immature); as_of already
        # past the full window but bars still missing -> data gap (terminal).
        return "no_future_bars" if mature_cut else "immature"

    entry_px = float(bars[entry_pos].o)
    side = "long"
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0
    e1, r1, net1 = _simulate(side, entry_px, v1_stop, entry_pos, bars,
                             HOLD_BARS, fee_pct)
    e2, r2, net2 = _simulate(side, entry_px, v2_stop, entry_pos, bars,
                             HOLD_BARS, fee_pct)

    rec["cf_entry_px"] = round(entry_px, 6)
    rec["cf_side"] = side
    rec["cf_entry"] = "bar_open"
    rec["cf_side_default"] = True
    rec["cf_hold_bars"] = HOLD_BARS
    rec["cf_v1_exit_px"] = round(e1, 6)
    rec["cf_v2_exit_px"] = round(e2, 6)
    rec["cf_v1_pnl_pct"] = round(net1, 4)
    rec["cf_v2_pnl_pct"] = round(net2, 4)
    rec["cf_v1_exit_reason"] = r1
    rec["cf_v2_exit_reason"] = r2
    edge = net1 - net2
    rec["pnl_pct"] = round(edge, 4)
    rec["fee_bps"] = ROUND_TRIP_FEE_BPS
    rec["entry_px_source"] = "bar_open"
    rec["outcome_source"] = "historical_replay"
    rec["outcome"] = "win" if edge > 0 else "loss"
    return rec["outcome"]


def run(*, file: str = FILE, out: str = DEFAULT_OUT,
        write: bool = False, force: bool = False,
        as_of_ms: Optional[int] = None, max_age_hours: Optional[float] = None,
        use_cache: bool = True, sleep_s: float = 0.0,
        cache_file: Optional[str] = None) -> dict[str, Any]:
    """Core entrypoint (importable). Returns a stats dict. Never raises on a
    per-record grading failure — the offending record is counted as an error.
    """
    if cache_file:
        hc.set_cache_file(cache_file)

    recs = _load_inputs(file)
    cutoff = ((time.time() - max_age_hours * 3600)
              if max_age_hours else None)

    produced: list[dict[str, Any]] = []
    counts = {"input": len(recs), "skipped_terminal": 0, "skipped_cutoff": 0,
              "win": 0, "loss": 0, "immature": 0, "not_material": 0,
              "no_future_bars": 0, "error": 0}

    for r in recs:
        prior = r.get("outcome")
        if prior in _TERMINAL and not force:
            counts["skipped_terminal"] += 1
            continue
        t0 = signal_ms(r)
        if cutoff and t0 and t0 / 1000 < cutoff:
            counts["skipped_cutoff"] += 1
            continue
        origin = r.pop("_origin_file", "")
        try:
            outcome = grade_record(r, as_of_ms=as_of_ms, use_cache=use_cache)
        except Exception as e:  # best-effort batch: count, don't abort
            counts["error"] += 1
            r["_origin_file"] = origin
            counts.setdefault("errors", []).append(repr(e))
            continue
        r["origin_file"] = origin
        if outcome in MATURE_OUTCOMES:
            counts[outcome] += 1
            produced.append(r)
        elif outcome == "immature":
            counts["immature"] += 1
        elif outcome == "not_material":
            counts["not_material"] += 1
        elif outcome == "no_future_bars":
            counts["no_future_bars"] += 1
        if sleep_s and produced:
            time.sleep(sleep_s)

    if write and produced:
        # Isolated append product. Merge with any prior backfill output,
        # de-duping on record identity; re-runs replace stale replays.
        existing: dict[tuple[Any, ...], dict[str, Any]] = {}
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        old = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    existing[_identity(old)] = old
        for r in produced:
            existing[_identity(r)] = r
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in sorted(existing.values(),
                            key=lambda x: x.get("ts", 0)):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, out)
        hc.flush_disk_cache()

    counts["produced"] = len(produced)
    counts["written"] = len(produced) if write else 0
    counts["out_path"] = out if write else None
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=FILE, help="live atr_regime_calib JSONL "
                    "(rotations .1..5 are also read)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="isolated backfill product (never the live file)")
    ap.add_argument("--write", action="store_true",
                    help="write the isolated backfill JSONL (default dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="re-grade records that already have outcomes")
    ap.add_argument("--max-age-hours", type=float, default=None)
    ap.add_argument("--as-of", type=int, default=None,
                    help="PIT cutoff in epoch ms (testing/replay determinism)")
    ap.add_argument("--cache-file", default=None,
                    help="historical candle disk cache path")
    ap.add_argument("--sleep", type=float, default=0.0)
    args = ap.parse_args()

    if os.path.abspath(args.out) == os.path.abspath(args.file):
        print("refusing: --out must differ from the live --file")
        return 2

    stats = run(file=args.file, out=args.out, write=args.write,
                force=args.force, as_of_ms=args.as_of,
                max_age_hours=args.max_age_hours, cache_file=args.cache_file,
                sleep_s=args.sleep)
    graded = stats["win"] + stats["loss"]
    print(f"=== {ARM} historical backfill (pilot) ===")
    print(f"input records (deduped across rotations): {stats['input']}")
    print(f"skipped already-terminal: {stats['skipped_terminal']}")
    print(f"mature graded: {graded}  "
          f"(arm-harmful/win {stats['win']} / arm-beneficial/loss "
          f"{stats['loss']})  immature {stats['immature']}  not_material "
          f"{stats['not_material']}  no_future_bars "
          f"{stats['no_future_bars']}  errors {stats['error']}")
    if graded:
        print(f"arm-beneficial rate (v2 outperforms v1, outcome=loss): "
              f"{100*stats['loss']/graded:.1f}%")
    if stats["error"] and stats.get("errors"):
        for e in stats["errors"][:5]:
            print(f"  error: {e}")
    if args.write:
        print(f"written (isolated): {args.out}  rows={stats['written']}")
    else:
        print("(dry-run; pass --write to emit the isolated backfill file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
