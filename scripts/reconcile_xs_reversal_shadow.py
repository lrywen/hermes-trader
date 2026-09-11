#!/usr/bin/env python3
"""Reconcile xs_reversal oversold-bounce SHADOW signals with forward returns.

The xs_reversal M1 edge (archive/scripts/backtest_xs_reversal.py) was validated
on FIXED forward windows — 72h WR / mean and 168h WR / mean over the would-be
LONG entry — NOT on a DSL trailing walk. This grader preserves that exact
geometry so shadow accrual is comparable to the backtest: for each trigger it
fetches 1h candles after the signal bar and records the sign/magnitude of the
forward move at 24h / 72h / 168h.

It reads the live file AND its size-rotated siblings (.1/.2/...), skips records
already graded (unless --force), and by default is a dry run; --write stamps
``outcome`` plus the forward grid back into the file each record came from.

Pure paper reconciliation: never places an order, never touches a live
decision. Read-only on the market (1h candleSnapshot).

Usage:
    python3 scripts/reconcile_xs_reversal_shadow.py
    python3 scripts/reconcile_xs_reversal_shadow.py --write
    python3 scripts/reconcile_xs_reversal_shadow.py --file /data/xs_reversal_shadow.jsonl
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

from hermes_trader.client.hl_client import _http_post  # noqa: E402

DEFAULT_FILE = "/data/xs_reversal_shadow.jsonl"
FILE = os.environ.get("HERMES_XS_REVERSAL_SHADOW_FILE", DEFAULT_FILE)
FWD_HOURS = (24, 72, 168)
MIN_BARS_AFTER = 200          # require a little warm/headroom beyond 168h

# Mature terminal outcomes the grader counts. Legacy aliases from before the
# vocabulary alignment ("winner"/"loser") are accepted as already-graded and
# normalized to "win"/"loss" on --write so historical rows don't stay invisible.
MATURE_OUTCOMES = ("win", "loss")
_LEGACY_OUTCOME = {"winner": "win", "loser": "loss"}


def _signal_ms(rec: dict[str, Any]) -> Optional[int]:
    # xs_reversal stores an int millisecond epoch under "timestamp".
    ts = rec.get("timestamp")
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


def _forward_closes(coin: str, start_ms: int, hours: int) -> Optional[list[float]]:
    """1h closes from the signal bar through start+hours (inclusive)."""
    end = start_ms + (hours + 4) * 3600_000
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": start_ms - 3600_000, "endTime": end}}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list) or not raw:
        return None
    rows = sorted(raw, key=lambda c: int(c["t"]))
    return [float(c["c"]) for c in rows]


def grade(rec: dict[str, Any]) -> str:
    """Return outcome bucket; mutate rec with forward grid on success."""
    coin = rec.get("coin")
    entry = rec.get("entry_px")
    t0 = _signal_ms(rec)
    if not coin:
        return "no_coin"
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return "no_entry_px"
    if entry <= 0:
        return "no_entry_px"
    if not t0:
        return "bad_timestamp"

    closes = _forward_closes(coin, t0, max(FWD_HOURS))
    if not closes:
        return "no_future_bars"

    # closes[0] is the bar at/just before t0; forward H-bar close = index H+1.
    grid: dict[str, Any] = {}
    for h in FWD_HOURS:
        idx = h + 1
        if idx >= len(closes):
            grid[f"fwd{h}h_pct"] = None
            continue
        px = closes[idx]
        pct = (px - entry) / entry * 100.0
        grid[f"fwd{h}h_pct"] = round(pct, 4)
        grid[f"fwd{h}h_px"] = px

    rec["forward"] = grid
    v72 = grid.get("fwd72h_pct")
    if v72 is None:
        # API returned bars but the 72h bar has not printed yet — too young.
        return "immature"
    rec["pnl_pct"] = v72
    # Vocabulary aligned with the other reconcile arms (pullback/change/tuning)
    # and the grader shadow_grade._window_stats, which only counts outcomes in
    # ("win", "loss"). The legacy "winner"/"loser" values were invisible to the
    # grader (mature outcome backfill always read 0); normalize via MATURE_OUTCOMES.
    return "win" if v72 > 0 else "loss"


def _iter_files(primary: str) -> list[str]:
    files = sorted(glob.glob(primary + ".*"), reverse=True)
    if os.path.exists(primary):
        files.append(primary)
    # de-dupe preserving order
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f); out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=FILE)
    ap.add_argument("--write", action="store_true",
                    help="persist outcomes into each JSONL (default dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="re-grade records that already have outcomes")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="only grade signals younger than N hours")
    args = ap.parse_args()

    files = _iter_files(args.file)
    if not files:
        print(f"no shadow files for {args.file}")
        return 1

    total = graded = winners = 0
    by_candidate = {True: [0, 0], False: [0, 0]}   # [n, wins]
    by_macro: dict[str, list[int]] = {}
    cutoff = (time.time() - args.max_age_hours * 3600) if args.max_age_hours else None

    for path in files:
        recs = []
        changed = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                prior = r.get("outcome")
                if (prior in MATURE_OUTCOMES or prior in _LEGACY_OUTCOME) and not args.force:
                    # already graded — normalize legacy vocabulary on --write,
                    # still fold into summary.
                    if prior in _LEGACY_OUTCOME and args.write:
                        r["outcome"] = _LEGACY_OUTCOME[prior]
                        changed += 1
                    # already graded — still fold into summary
                    v = r.get("pnl_pct")
                    if isinstance(v, (int, float)):
                        winners += v > 0
                        graded += 1
                        _tally(by_candidate, by_macro, r, v > 0)
                    recs.append(r)
                    continue
                t0 = _signal_ms(r)
                if cutoff and t0 and t0/1000 < cutoff:
                    recs.append(r)
                    continue
                outcome = grade(r)
                r["outcome"] = outcome
                if outcome in MATURE_OUTCOMES:
                    changed += 1
                    graded += 1
                    win = outcome == "win"
                    winners += win
                    _tally(by_candidate, by_macro, r, win)
                recs.append(r)
        if args.write and changed:
            with open(path, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  {path}: total={len(recs)} newly_graded={changed}"
              f"{' [WRITTEN]' if args.write and changed else ''}")

    print(f"\n=== xs_reversal shadow reconciliation ===")
    print(f"records scanned: {total}   mature+graded: {graded}")
    if graded:
        print(f"overall 72h win-rate: {100*winners/graded:.1f}% ({winners}W/{graded-winners}L)")
    nc, nw = by_candidate[True]
    if nc:
        print(f"is_candidate=True (M1 validated cell): n={nc}  72h WR={100*nw/nc:.1f}%")
    no, nwo = by_candidate[False]
    if no:
        print(f"is_candidate=False (trigger only):     n={no}  72h WR={100*nwo/no:.1f}%")
    if by_macro:
        print("by macro_regime label:")
        for k in sorted(by_macro):
            n, w = by_macro[k]
            print(f"  {k:14s} n={n:3d}  72h WR={100*w/n:5.1f}%")
    if not args.write:
        print("\n(dry-run; pass --write to persist outcomes into the JSONL)")
    return 0


def _tally(by_candidate, by_macro, r, win):
    c = bool(r.get("is_candidate"))
    by_candidate[c][0] += 1
    by_candidate[c][1] += int(win)
    k = str(r.get("macro_regime") or "?")
    bucket = by_macro.setdefault(k, [0, 0])
    bucket[0] += 1
    bucket[1] += int(win)


if __name__ == "__main__":
    raise SystemExit(main())
