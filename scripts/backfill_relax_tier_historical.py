#!/usr/bin/env python3
"""Historical backfiller for the relax_tier shadow arms (rt_relax45 /
rt_weak_rsi70 / rt_no_adx20 verdicts on passed ta_late_entry decisions).

Replaces the *wait* for forward bars to print in real time with a
point-in-time replay over immutable closed 4h history (audited 2026-09-14:
pinned historical ranges and live recent-window fetches return byte-identical
OHLC for closed bars).

Scope / safety contract (relax_tier verdicts ONLY):
  * dry-run by default; ``--write`` writes an ISOLATED product
    (``relax_tier_shadow.backfill.jsonl``), never the live JSONL —
    use ``--out`` to redirect;
  * eligibility mirrors reconcile_relax_tier_shadow._eligible: layer "gate"
    (missing layer treated as "gate"), ``blocked is False``, and at least one
    rt_*_would_block verdict field present;
  * only records without ``rt_graded`` are graded (idempotent); the live
    ta_late_entry file and its .1..5 rotations are read-only inputs here;
  * every produced row is tagged ``rt_outcome_source="historical_replay"`` so
    the grader can stratify;
  * point-in-time: only bars CLOSED at the grading instant are used, via
    hermes_trader.data.historical_candles.closed_bars_as_of;
  * geometry preserved from reconcile_relax_tier_shadow (post
    timestamp-anchor fix): B0 is the 4h bar whose OPEN is the 4h boundary
    containing t0; the forward-h close is the close of the bar opening
    (h//4 + 1) bars after B0; side-aware net of 5bps round-trip fees;
    mae_pct = worst side-aware close from B0 onward; primary 72h.

Pure paper tooling: sets HERMES_BACKTEST=1, never orders, never writes live
state apart from the regenerable candle cache.

Usage:
    python3 scripts/backfill_relax_tier_historical.py          # dry-run
    python3 scripts/backfill_relax_tier_historical.py --write
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

from hermes_trader.data import historical_candles as hc

DEFAULT_FILE = "/data/ta_late_entry_shadow.jsonl"
DEFAULT_OUT = "/data/relax_tier_shadow.backfill.jsonl"
FILE = os.environ.get("HERMES_TA_LATE_ENTRY_SHADOW_FILE", DEFAULT_FILE)

ARM = "relax_tier"
INTERVAL = "4h"
FWD_HOURS = (6, 24, 72)
PRIMARY_HOURS = 72
BAR_MS = hc.INTERVAL_MS[INTERVAL]
CONTEXT_BARS = 1
ROUND_TRIP_FEE_BPS = 5.0

# rt arm key -> would_block field (mirrors reconcile_relax_tier_shadow.ARMS).
ARMS = (
    ("rt_relax45", "rt_relax45_would_block"),
    ("rt_weak_rsi70", "rt_weak_rsi70_would_block"),
    ("rt_no_adx20", "rt_no_adx20_would_block"),
)
GRADED_FLAG = "rt_graded"
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


def _eligible(r: dict[str, Any]) -> bool:
    """Passed order-time decision carrying at least one rt verdict."""
    if r.get("layer", "gate") != "gate" or r.get("blocked") is not False:
        return False
    return any(r.get(fld) is not None for _, fld in ARMS)


def grade_record(rec: dict[str, Any], *, as_of_ms: Optional[int] = None,
                 use_cache: bool = True) -> str:
    """Grade one record with a pinned historical fetch; mutate rec in place.

    Returns the terminal/non-terminal outcome string. Mirrors
    reconcile_relax_tier_shadow.grade geometry (timestamp-anchored) but via
    the PIT data layer."""
    coin = rec.get("coin")
    if not coin:
        return "no_coin"
    side = rec.get("side") if rec.get("side") in ("long", "short") else "long"
    try:
        entry = float(rec.get("entry_px") or 0)
    except (TypeError, ValueError):
        return "no_entry_px"
    if entry <= 0:
        return "no_entry_px"
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
    by_t = {b.t: b.c for b in bars}

    sign = 1.0 if side == "long" else -1.0
    fee_pct = ROUND_TRIP_FEE_BPS / 100.0
    grid: dict[str, Any] = {}
    for h in FWD_HOURS:
        px = by_t.get(g0 + (h // 4 + 1) * BAR_MS)
        if px is None:
            grid[f"fwd{h}h_pct"] = None
            continue
        net = sign * (px - entry) / entry * 100.0 - fee_pct
        grid[f"fwd{h}h_pct"] = round(net, 4)
        grid[f"fwd{h}h_px"] = px
    # Worst adverse side-aware close across the window from B0 onward.
    window = [c for t, c in by_t.items() if t >= g0]
    if not window:
        return "no_future_bars"
    grid["mae_pct"] = round(
        min(sign * (c - entry) / entry * 100.0 - fee_pct for c in window), 4)

    rec["rt_forward"] = grid
    primary = grid.get(f"fwd{PRIMARY_HOURS}h_pct")
    if primary is None:
        return "immature"
    rec["rt_pnl_pct"] = primary
    rec["rt_outcome_source"] = "historical_replay"
    rec["rt_outcome"] = "win" if primary > 0 else "loss"
    return rec["rt_outcome"]


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
    counts = {"input": len(recs), "skipped_ineligible": 0,
              "skipped_terminal": 0, "skipped_cutoff": 0,
              "win": 0, "loss": 0, "immature": 0, "no_future_bars": 0,
              "error": 0}

    for r in recs:
        if not _eligible(r):
            counts["skipped_ineligible"] += 1
            continue
        if r.get(GRADED_FLAG) and not force:
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
            r[GRADED_FLAG] = True
            counts[outcome] += 1
            produced.append(r)
        elif outcome == "immature":
            counts["immature"] += 1
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
                    "carrying rt verdicts (rotations .1..5 are also read)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="isolated backfill product (never the live file)")
    ap.add_argument("--write", action="store_true",
                    help="write the isolated backfill JSONL (default dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="re-grade records already stamped rt_graded")
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
    print(f"=== {ARM} historical backfill ===")
    print(f"input records (deduped across rotations): {stats['input']}")
    print(f"skipped ineligible: {stats['skipped_ineligible']}  "
          f"already-graded: {stats['skipped_terminal']}  "
          f"guard-railed: {stats.get('skipped_guard', 0)}")
    print(f"mature graded: {graded}  "
          f"(win {stats['win']} / loss {stats['loss']})  "
          f"immature {stats['immature']}  no_future_bars "
          f"{stats['no_future_bars']}  errors {stats['error']}")
    if graded:
        print(f"net {PRIMARY_HOURS}h win-rate: "
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
