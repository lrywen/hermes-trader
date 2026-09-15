#!/usr/bin/env python3
"""Historical backfiller for the ta_late_entry shadow arm (blocked vetoes).

Replaces the *wait* for forward bars to print in real time with a
point-in-time replay over immutable closed 4h history (audited 2026-09-14:
pinned historical ranges and live recent-window fetches return byte-identical
OHLC for closed bars).

Scope / safety contract (ta_late_entry ONLY):
  * dry-run by default; ``--write`` writes an ISOLATED product
    (``ta_late_entry_shadow.backfill.jsonl``), never the live JSONL —
    use ``--out`` to redirect;
  * only records with ``blocked is True`` are counterfactual trades
    (mirrors reconcile_ta_late_entry_shadow; missing layer treated as
    "gate"); only records with ``outcome`` null/absent are graded
    (idempotent); the live file and its .1..5 rotations are read-only
    inputs here;
  * every produced row is tagged ``outcome_source="historical_replay"`` plus
    ``entry_px_source`` so the grader can stratify;
  * point-in-time: only bars CLOSED at the grading instant are used, via
    hermes_trader.data.historical_candles.closed_bars_as_of;
  * geometry preserved from reconcile_ta_late_entry_shadow: B1 = first 4h
    bar whose open >= signal ts; exit at the close of bar index
    b1_idx - 2 + hold_bars (hold_bars=2 => B1.close); prefilter records
    without entry_px approximate the chase fill at B0.close; mae_pct =
    worst side-aware close over [B0, exit]; net of 5bps round-trip fees.

Pure paper tooling: sets HERMES_BACKTEST=1, never orders, never writes live
state apart from the regenerable candle cache.

Usage:
    python3 scripts/backfill_ta_late_entry_historical.py          # dry-run
    python3 scripts/backfill_ta_late_entry_historical.py --write
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.data import historical_candles as hc  # noqa: E402

DEFAULT_FILE = "/data/ta_late_entry_shadow.jsonl"
DEFAULT_OUT = "/data/ta_late_entry_shadow.backfill.jsonl"
FILE = os.environ.get("HERMES_TA_LATE_ENTRY_SHADOW_FILE", DEFAULT_FILE)

ARM = "ta_late_entry"
INTERVAL = "4h"
HOLD_BARS = 2
BAR_MS = hc.INTERVAL_MS[INTERVAL]
# Bars fetched start CONTEXT_BARS before the signal 4h bar so B0 (the bar
# forming at signal time, or the pre-bar for aligned timestamps) is present.
CONTEXT_BARS = 2
ROUND_TRIP_FEE_BPS = 5.0

MATURE_OUTCOMES = ("win", "loss")
_TERMINAL = MATURE_OUTCOMES + (
    "no_coin", "no_entry_px", "bad_timestamp", "no_future_bars")


def signal_ms(rec: dict[str, Any]) -> Optional[int]:
    ts = rec.get("timestamp", rec.get("ts"))
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
                # Dedupe across rotations: (coin, signal ts, entry) identity.
                rid = (r.get("coin"), r.get("timestamp"), r.get("entry_px"))
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                r["_origin_file"] = os.path.basename(path)
                recs.append(r)
    return recs


def grade_record(rec: dict[str, Any], *, hold_bars: int = HOLD_BARS,
                 as_of_ms: Optional[int] = None,
                 use_cache: bool = True) -> str:
    """Grade one record with a pinned historical fetch; mutate rec in place.

    Returns the terminal/non-terminal outcome string. Mirrors
    reconcile_ta_late_entry_shadow geometry but via the PIT data layer."""
    coin = rec.get("coin")
    if not coin:
        return "no_coin"
    side = rec.get("side") if rec.get("side") in ("long", "short") else "long"
    t0 = signal_ms(rec)
    if not t0:
        return "bad_timestamp"

    as_of = as_of_ms if as_of_ms is not None else int(time.time() * 1000)
    g0 = t0 - (t0 % BAR_MS)
    bars = hc.closed_bars_as_of(str(coin), INTERVAL,
                                g0 - CONTEXT_BARS * BAR_MS, as_of,
                                use_cache=use_cache)
    if not bars:
        return "no_future_bars"

    # B1 = first bar whose open >= signal ts (mirrors _find_entry_bar).
    b1_idx = -1
    for i, b in enumerate(bars):
        if b.t >= t0:
            b1_idx = i
            break
    exit_idx = b1_idx - 2 + hold_bars
    if b1_idx < 1 or exit_idx >= len(bars) or exit_idx < 0:
        return "no_future_bars"

    try:
        entry_px = float(rec.get("entry_px") or 0)
    except (TypeError, ValueError):
        entry_px = 0.0
    if entry_px <= 0:
        # Prefilter records carry no entry px: approximate the chase fill at
        # B0.close, the bar that was forming at signal time.
        entry_px = float(bars[b1_idx - 1].c)
        entry_src = "b0_close"
    else:
        entry_src = "recorded"
    if entry_px <= 0:
        return "no_entry_px"

    sign = 1.0 if side == "long" else -1.0
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0
    exit_px = bars[exit_idx].c
    # Most adverse excursion over the hold, starting from B0.close.
    mae_pct = 0.0
    for j in range(max(b1_idx - 1, 0), exit_idx + 1):
        move_pct = sign * (bars[j].c - entry_px) / entry_px * 100.0
        if move_pct < mae_pct:
            mae_pct = move_pct
    gross_pct = sign * (exit_px - entry_px) / entry_px * 100.0
    net_pct = gross_pct - fee_pct * 100.0

    rec["entry_px"] = round(entry_px, 6)
    rec["exit_px"] = round(exit_px, 6)
    rec["pnl_pct"] = round(net_pct, 4)
    rec["mae_pct"] = round(mae_pct, 4)
    rec["hold_bars"] = hold_bars
    notional = rec.get("trade_notional_usd")
    if notional:
        rec["pnl_usd"] = round(net_pct / 100.0 * float(notional), 2)
    rec["entry_px_source"] = entry_src
    rec["outcome_source"] = "historical_replay"
    rec["outcome"] = "win" if net_pct > 0 else "loss"
    return rec["outcome"]


def run(*, file: str = FILE, out: str = DEFAULT_OUT,
        write: bool = False, force: bool = False, hold_bars: int = HOLD_BARS,
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
    counts = {"input": len(recs), "skipped_not_veto": 0,
              "skipped_terminal": 0, "skipped_cutoff": 0,
              "win": 0, "loss": 0, "no_future_bars": 0, "error": 0}

    for r in recs:
        # Only blocked (would-veto) records are counterfactual trades.
        if r.get("blocked") is not True:
            counts["skipped_not_veto"] += 1
            continue
        prior = r.get("outcome")
        if prior is not None and not force:
            counts["skipped_terminal"] += 1
            continue
        t0 = signal_ms(r)
        if cutoff and t0 and t0 / 1000 < cutoff:
            counts["skipped_cutoff"] += 1
            continue
        origin = r.pop("_origin_file", "")
        try:
            outcome = grade_record(r, hold_bars=hold_bars, as_of_ms=as_of_ms,
                                   use_cache=use_cache)
        except Exception as e:  # best-effort batch: count, don't abort
            counts["error"] += 1
            r["_origin_file"] = origin
            counts.setdefault("errors", []).append(repr(e))
            continue
        r["origin_file"] = origin
        if outcome in MATURE_OUTCOMES:
            counts[outcome] += 1
            produced.append(r)
        elif outcome == "no_future_bars":
            counts["no_future_bars"] += 1
        else:
            counts.setdefault("skipped_guard", 0)
            counts["skipped_guard"] += 1
        if sleep_s and produced:
            time.sleep(sleep_s)

    if write and produced:
        # Isolated append product. Merge with any prior backfill output,
        # de-duping on (coin, ts, entry); re-runs replace stale replays.
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
                    existing[(old.get("coin"), old.get("timestamp"),
                              old.get("entry_px"))] = old
        for r in produced:
            existing[(r.get("coin"), r.get("timestamp"),
                      r.get("entry_px"))] = r
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in sorted(existing.values(),
                            key=lambda x: str(x.get("timestamp", ""))):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, out)
        hc.flush_disk_cache()

    counts["produced"] = len(produced)
    counts["written"] = len(produced) if write else 0
    counts["out_path"] = out if write else None
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=FILE, help="live ta_late_entry JSONL "
                    "(rotations .1..5 are also read)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="isolated backfill product (never the live file)")
    ap.add_argument("--write", action="store_true",
                    help="write the isolated backfill JSONL (default dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="re-grade records that already have outcomes")
    ap.add_argument("--hold-bars", type=int, default=HOLD_BARS,
                    help="4h bars to hold the counterfactual trade (default 2)")
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
                force=args.force, hold_bars=args.hold_bars,
                as_of_ms=args.as_of, max_age_hours=args.max_age_hours,
                cache_file=args.cache_file, sleep_s=args.sleep)
    graded = stats["win"] + stats["loss"]
    print(f"=== {ARM} historical backfill ===")
    print(f"input records (deduped across rotations): {stats['input']}")
    print(f"skipped not-veto: {stats['skipped_not_veto']}  "
          f"already-terminal: {stats['skipped_terminal']}  "
          f"guard-railed: {stats.get('skipped_guard', 0)}")
    print(f"mature graded: {graded}  "
          f"(win {stats['win']} / loss {stats['loss']})  "
          f"no_future_bars {stats['no_future_bars']}  "
          f"errors {stats['error']}")
    if graded:
        print(f"net win-rate ({args.hold_bars}x4h hold): "
              f"{100*stats['win']/graded:.1f}%")
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
