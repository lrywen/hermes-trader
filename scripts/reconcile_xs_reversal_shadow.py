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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.client.hl_client import _http_post

DEFAULT_FILE = "/data/xs_reversal_shadow.jsonl"
FILE = os.environ.get("HERMES_XS_REVERSAL_SHADOW_FILE", DEFAULT_FILE)
# Append-only, de-duplicated mature-outcome ledger (audit 2026-10-28). The raw
# shadow JSONL is size/day-rotated, so writing graded outcomes back only into
# those files lets evidence age out (the 60 signal-day gate became structurally
# unreachable). This ledger is NEVER rotated and keeps one compact row per
# (bar_ts, coin); reconcile is idempotent against it. It is the durable source
# for forward-evidence panels regardless of raw-log retention.
DEFAULT_LEDGER = "/data/xs_reversal_outcomes.ledger.jsonl"
FWD_HOURS = (24, 72, 168)
MIN_BARS_AFTER = 200          # require a little warm/headroom beyond 168h

# Round-trip cost in basis points used to turn the GROSS forward grid into the
# NET percent outcome the S2 dashboard / top_pct dual-count consume. Mirrors the
# research convention (9.26bp rt); overridable via --cost-bps. The net figure is
# stored on a SEPARATE top-level key (net_outcome) because "outcome" must stay a
# win/loss string for shadow_grade._window_stats (a dict there would break the
# grader and the already-graded skip logic).
DEFAULT_COST_BPS = 9.26

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


def _forward_closes(coin: str, start_ms: int, hours: int) -> Optional[dict[int, float]]:
    """1h closes keyed by bar-open ms, from start-1h through start+hours."""
    end = start_ms + (hours + 4) * 3600_000
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": start_ms - 3600_000, "endTime": end}}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list) or not raw:
        return None
    return {int(c["t"]): float(c["c"]) for c in raw}


def grade(rec: dict[str, Any], cost_bps: float = DEFAULT_COST_BPS) -> str:
    """Return outcome bucket; mutate rec with forward grid on success.

    Also stamps a numeric NET outcome on rec["net_outcome"] (percent, net of
    cost_bps round-trip) so read-only S2 evidence tools can consume it without
    re-fetching candles. "outcome" stays the win/loss string.
    """
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

    bars = _forward_closes(coin, t0, max(FWD_HOURS))
    if not bars:
        return "no_future_bars"

    # Timestamp-anchored grid: the forward-h close is the close of the bar
    # whose OPEN time is grid0 + h*step (grid0 = signal floored to the hour).
    # The old positional read (closes[h+1]) silently shifted the whole grid
    # whenever the API window had a head gap or an extra leading bar.
    step = 3600_000
    grid0 = t0 - (t0 % step)
    grid: dict[str, Any] = {}
    for h in FWD_HOURS:
        px = bars.get(grid0 + h * step)
        if px is None:
            grid[f"fwd{h}h_pct"] = None
            continue
        pct = (px - entry) / entry * 100.0
        grid[f"fwd{h}h_pct"] = round(pct, 4)
        grid[f"fwd{h}h_px"] = px

    rec["forward"] = grid
    v72 = grid.get("fwd72h_pct")
    if v72 is None:
        # API returned bars but the 72h bar has not printed yet — too young.
        return "immature"
    rec["pnl_pct"] = v72
    # Numeric net-of-cost outcome (PERCENT) for S2 dashboard / dual-count live
    # readers. Kept separate from the win/loss string under "outcome".
    net_grid: dict[str, Any] = {"cost_bps_rt": cost_bps}
    for h in FWD_HOURS:
        g = grid.get(f"fwd{h}h_pct")
        net_grid[f"fwd{h}_net_pct"] = (
            round(g - cost_bps / 100.0, 4) if g is not None else None)
    rec["net_outcome"] = net_grid
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


def _ledger_key(rec: dict[str, Any]) -> Optional[tuple[Any, Any]]:
    """Stable (bar_ts_ms, coin) identity for the append-only ledger."""
    t0 = _signal_ms(rec)
    coin = rec.get("coin")
    if not t0 or not coin:
        return None
    return (t0, coin)


def _load_ledger_keys(path: str) -> set[tuple[Any, Any]]:
    keys: set[tuple[Any, Any]] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = _ledger_key(r)
                if k is not None:
                    keys.add(k)
    except FileNotFoundError:
        return set()
    return keys


def _ledger_row(rec: dict[str, Any]) -> dict[str, Any]:
    """Compact durable row: identity + candidate/panic features + outcome."""
    net = rec.get("net_outcome") or {}
    # Normalize legacy vocabulary even on a dry run (the in-memory rec is only
    # rewritten to win/loss under --write, but the ledger always stores canonical
    # outcomes so future readers don't inherit winner/loser).
    outcome = rec.get("outcome")
    outcome = _LEGACY_OUTCOME.get(outcome, outcome)
    return {
        "timestamp": _signal_ms(rec),
        "coin": rec.get("coin"),
        "is_candidate": bool(rec.get("is_candidate")),
        "ext_pct": rec.get("ext_pct"),
        "ext_percentile": rec.get("ext_percentile"),
        "rsi14": rec.get("rsi14"),
        "macro_regime": rec.get("macro_regime"),
        "atr24_pct": rec.get("atr24_pct"),
        "atr_pctile": rec.get("atr_pctile"),
        "panic_gate_active": rec.get("panic_gate_active",
                                     rec.get("panic_gate_enabled")),
        "panic_gate_pass": rec.get("panic_gate_pass"),
        "panic_gate_reasons": rec.get("panic_gate_reasons"),
        "outcome": outcome,
        "pnl_pct": rec.get("pnl_pct"),
        "net_outcome": net,
        "graded_at": int(time.time() * 1000),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=FILE)
    ap.add_argument("--write", action="store_true",
                    help="persist outcomes into each JSONL (default dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="re-grade records that already have outcomes")
    ap.add_argument("--max-age-hours", type=float, default=None,
                    help="only grade signals younger than N hours")
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS,
                    help="round-trip cost in bp for net_outcome (default %(default)s)")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER,
                    help="append-only, de-duplicated mature-outcome ledger path")
    ap.add_argument("--no-ledger", action="store_true",
                    help="do not read/write the durable ledger")
    args = ap.parse_args()

    files = _iter_files(args.file)
    if not files:
        print(f"no shadow files for {args.file}")
        return 1

    # The durable ledger is written on EVERY run (not gated by --write): it is
    # the long-term evidence store that survives raw-log rotation. --write only
    # controls stamping outcomes back into the rotating raw JSONL files.
    ledger_path = None if args.no_ledger else args.ledger
    ledger_keys: set[tuple[Any, Any]] = set()
    if ledger_path:
        ledger_keys = _load_ledger_keys(ledger_path)
    ledger_new: list[dict[str, Any]] = []

    def _note_ledger(rec: dict[str, Any]) -> None:
        if not ledger_path:
            return
        k = _ledger_key(rec)
        if k is None or k in ledger_keys:
            return
        ledger_keys.add(k)
        ledger_new.append(_ledger_row(rec))

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
                        _note_ledger(r)
                    recs.append(r)
                    continue
                t0 = _signal_ms(r)
                if cutoff and t0 and t0/1000 < cutoff:
                    recs.append(r)
                    continue
                outcome = grade(r, args.cost_bps)
                r["outcome"] = outcome
                if outcome in MATURE_OUTCOMES:
                    changed += 1
                    graded += 1
                    win = outcome == "win"
                    winners += win
                    _tally(by_candidate, by_macro, r, win)
                    _note_ledger(r)
                recs.append(r)
        if args.write and changed:
            with open(path, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  {path}: total={len(recs)} newly_graded={changed}"
              f"{' [WRITTEN]' if args.write and changed else ''}")

    # Flush the durable append-only ledger (survives raw-log rotation). Follows
    # the same best-effort contract: a ledger write failure must not fail the
    # read-only reconciliation report.
    if ledger_path and ledger_new:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(ledger_path)),
                        exist_ok=True)
            with open(ledger_path, "a", encoding="utf-8") as f:
                for row in ledger_new:
                    f.write(json.dumps(row, ensure_ascii=False,
                                       allow_nan=False) + "\n")
            print(f"durable ledger: +{len(ledger_new)} rows -> {ledger_path}")
        except OSError as e:
            print(f"durable ledger: WRITE FAILED ({e})")

    print("\n=== xs_reversal shadow reconciliation ===")
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
