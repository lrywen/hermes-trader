#!/usr/bin/env python3
"""Reconcile daily_extension_cap SHADOW probes with forward long returns.

daily_extension_cap is an anti-chase ceiling for LONGS: a long whose 24h gain
exceeds the cap is (in shadow) "would-blocked". The probe records carry NO
entry price (coin/side/daily_change_pct/cap_pct/ext_would_block/timestamp only),
so unlike the price-based arms the counterfactual entry must be reconstructed:
the 1h candle close at the signal timestamp is used as the would-be long entry.

Counterfactual question: "had we chased that already-extended long at the
signal bar, what would the forward long return have been?" Geometry mirrors
reconcile_xs_reversal_shadow (the M1/M16 fixed-window convention): 1h closes
after the signal bar, mature at the 72h close; outcome win/loss = sign of the
72h forward return. ext_would_block is preserved so the grader (or an analyst)
can separate "would-blocked chase longs" from ordinary below-cap probes; the
grader's backfill denominator is all long probes, the harmful-cell is the
would_block=true subset.

Long-only by construction (the gate short-skips). data_missing rows (unknown
24h change) are not graded. Reads the live file AND its size-rotated siblings
(.1/.2/...), skips records already graded (unless --force), dry-run by default;
--write stamps outcome (+ fwd grid + pnl_pct) back into the source file.

Pure paper: never places an order, never touches a live decision. Read-only on
the market (1h candleSnapshot).

Usage:
    python3 scripts/reconcile_daily_extension_cap_shadow.py
    python3 scripts/reconcile_daily_extension_cap_shadow.py --write
    python3 scripts/reconcile_daily_extension_cap_shadow.py --file /data/daily_extension_cap_shadow.jsonl
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

from hermes_trader.client.hl_client import _http_post

DEFAULT_FILE = "/data/daily_extension_cap_shadow.jsonl"
FILE = os.environ.get("HERMES_DAILY_EXTENSION_CAP_SHADOW_FILE", DEFAULT_FILE)
FWD_HOURS = (24, 72, 168)
PRIMARY_FWD_HOUR = 72           # mature window, matches xs_reversal convention
MIN_BARS_AFTER = 200            # a little headroom beyond 168h

# Mature terminal outcomes the grader counts (win/loss vocabulary contract).
MATURE_OUTCOMES = ("win", "loss")


def _signal_ms(rec: dict[str, Any]) -> Optional[int]:
    # The gate writes an ISO-8601 UTC string "...Z"; tolerate epoch too.
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
    """1h closes beginning at the signal bar (index 0 = signal bar close)."""
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
    """Return outcome bucket; mutate rec with entry/fwd grid on success."""
    coin = rec.get("coin")
    t0 = _signal_ms(rec)
    if not coin:
        return "no_coin"
    if not t0:
        return "bad_timestamp"
    # The gate is long-only; never grade anything else defensively.
    if rec.get("side") not in (None, "long"):
        return "side_skip"
    # A row with an unknown 24h change has no actionable counterfactual.
    if rec.get("state") == "data_missing" or rec.get("daily_change_pct") is None:
        return "data_missing"

    closes = _forward_closes(coin, t0, max(FWD_HOURS))
    if not closes:
        return "no_future_bars"

    # closes[0] is the pre-bar (startTime = t0-1h); the signal bar close is
    # index 1 and is the would-be chase entry. Fall back to closes[0] only if
    # the exchange returned a single bar (degenerate, but still an entry).
    if len(closes) >= 2:
        entry = closes[1]
    else:
        entry = closes[0]
    if not entry or entry <= 0:
        return "no_entry_px"
    rec["entry_px"] = entry

    grid: dict[str, Any] = {}
    for h in FWD_HOURS:
        idx = h + 1                    # signal bar is index 1
        if idx < len(closes) and closes[idx] > 0:
            pct = (closes[idx] - entry) / entry * 100.0
            grid[f"fwd{h}h_pct"] = round(pct, 4)
            grid[f"fwd{h}h_px"] = closes[idx]
        else:
            grid[f"fwd{h}h_pct"] = None
    rec["forward"] = grid

    v = grid.get(f"fwd{PRIMARY_FWD_HOUR}h_pct")
    if v is None:
        # The 72h bar has not printed yet — signal too young.
        return "immature"
    rec["pnl_pct"] = v
    return "win" if v > 0 else "loss"


def _iter_files(primary: str) -> list[str]:
    files = sorted(glob.glob(primary + ".*"), reverse=True)
    if os.path.exists(primary):
        files.append(primary)
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _tally(buckets: dict[Any, list[int]], key: Any, win: bool) -> None:
    b = buckets.setdefault(str(key), [0, 0])
    b[0] += 1
    b[1] += int(win)


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
    # [n, wins] keyed by ext_would_block (the blocked chase-long subset is True)
    by_block: dict[str, list[int]] = {}
    cutoff = (time.time() - args.max_age_hours * 3600) if args.max_age_hours else None

    for path in files:
        recs: list[dict[str, Any]] = []
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
                if r.get("outcome") in MATURE_OUTCOMES and not args.force:
                    v = r.get("pnl_pct")
                    if isinstance(v, (int, float)):
                        graded += 1
                        winners += v > 0
                        _tally(by_block, r.get("ext_would_block"), v > 0)
                    recs.append(r)
                    continue
                t0 = _signal_ms(r)
                if cutoff and t0 and t0 / 1000 < cutoff:
                    recs.append(r)
                    continue
                outcome = grade(r)
                r["outcome"] = outcome
                if outcome in MATURE_OUTCOMES:
                    changed += 1
                    graded += 1
                    win = outcome == "win"
                    winners += win
                    _tally(by_block, r.get("ext_would_block"), win)
                recs.append(r)
        if args.write and changed:
            with open(path, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  {path}: total={len(recs)} newly_graded={changed}"
              f"{' [WRITTEN]' if args.write and changed else ''}")

    print("\n=== daily_extension_cap shadow reconciliation ===")
    print(f"records scanned: {total}   mature+graded: {graded}")
    if graded:
        print(f"overall {PRIMARY_FWD_HOUR}h long win-rate: "
              f"{100*winners/graded:.1f}% ({winners}W/{graded-winners}L)")
    for key in ("True", "False", "None"):
        n, w = by_block.get(key, [0, 0])
        if n:
            label = {"True": "would-block (extended chase)",
                     "False": "below cap (allowed)",
                     "None": "unknown ext_would_block"}[key]
            print(f"  {label:32s} n={n:3d}  {PRIMARY_FWD_HOUR}h WR={100*w/n:5.1f}%")
    if not args.write:
        print("\n(dry-run; pass --write to persist outcomes into the JSONL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
