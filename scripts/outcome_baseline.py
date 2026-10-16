#!/usr/bin/env python3
"""Baseline rebuild for the 2026-10-16 problem list (#1/#2/#6).

Reads the live shadow book and reports the REAL numbers behind three claims
that could not be reproduced from the ledger earlier:

  #1  stop-loss slippage "eats 66% of losses" (-$1.840 / -$2.795)
  #2  over-trading: "37 trades / 2.47 per day / 900 per year"
  #6  concurrency: "avg 0.69 open positions, max 2; net beta +0.10x/+0.86x"

Definitions (grounded in what the ledger actually records):
  * Fixed slippage $ = notional_usd x slippage_pct/100   (taker 5bp default)
  * Gap-through %   = spot_pct - stop_cap (parsed from the max_loss reason
                       ">= X.XX% spot cap"); >0 means price gapped THROUGH the
                       stop before the confirm window elapsed. Gap $ =
                       notional_usd x gap% /100.
  * Turnover         = close fills per day, annualised x365. Reports the whole
                       ledger, the last 15d and the last 30d so the window
                       choice no longer hides the answer.
  * Concurrency      = reconstructed open-position count over time from the
                       open/close fill timeline (cumulative open - close).
  * Net exposure     = mean/max of (long-notional - short-notional)/equity as
                       sampled each close (no per-coin beta in the system).

Read-only: never writes config, never places orders, never mutates the book.
Usage:
  python scripts/outcome_baseline.py
  python scripts/outcome_baseline.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any

DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
BOOK_PATH = os.path.join(DATA_DIR, ".shadow-book.json")

# reason shape: "max_loss (2.59% spot / 25.9% ROE >= 2.50% spot cap ..."
_STOP_CAP_RE = re.compile(r">=\s*([\d.]+)\s*%\s*spot\s*cap", re.IGNORECASE)


# --------------------------------------------------------------------------- load

def load_book() -> dict[str, Any]:
    with open(BOOK_PATH) as fh:
        return json.load(fh)


def closes(book: dict[str, Any], acct: str = "taker") -> list[dict[str, Any]]:
    return [f for f in book["accounts"][acct]["fills"] if f.get("type") == "close"]


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- metrics

def _reason_group(reason: str) -> str:
    return str(reason).split(" (")[0].split(";")[0].strip() or "unknown"


def _stop_cap(reason: str) -> float | None:
    m = _STOP_CAP_RE.search(str(reason))
    return float(m.group(1)) if m else None


def group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "win_rate": None, "mean_pnl_usd": 0.0,
                "total_pnl_usd": 0.0, "gross_win_usd": 0.0, "gross_loss_usd": 0.0,
                "profit_factor": None, "worst_loss_usd": 0.0}
    pnls = [_f(r.get("realized_pnl_usd")) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw = sum(wins)
    gl = -sum(losses)
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100.0, 1) if n else None,
        "mean_pnl_usd": round(sum(pnls) / n, 4),
        "total_pnl_usd": round(sum(pnls), 4),
        "gross_win_usd": round(gw, 4),
        "gross_loss_usd": round(gl, 4),
        "profit_factor": round(gw / gl, 3) if gl > 0 else None,
        "worst_loss_usd": round(min(pnls), 4),
    }


def slippage_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fixed-slippage cost + gap-through overstop for max_loss exits.

    spot_pct is NEGATIVE for a long loss (e.g. -7.78 = price fell 7.78%).
    gap-through is the amount by which |spot_pct| EXCEEDED the stop cap before
    the confirm window elapsed — the true "stop-slippage" cost the problem
    list tried to quantify.
    """
    fixed_usd = 0.0
    gap_usd = 0.0
    gap_rows = 0
    for r in rows:
        notional = _f(r.get("notional_usd"))
        slip_pct = _f(r.get("slippage_pct"))
        # max_loss closes historically omit slippage_pct (None); treat missing
        # as no fixed-slip component rather than guessing.
        if slip_pct:
            fixed_usd += notional * slip_pct / 100.0
        if _reason_group(r.get("reason", "")) == "max_loss":
            cap = _stop_cap(r.get("reason", ""))
            spot = abs(_f(r.get("spot_pct")))
            if cap is not None and spot > cap:
                gap = spot - cap
                gap_usd += notional * gap / 100.0
                gap_rows += 1
    losses = sum(min(_f(r.get("realized_pnl_usd")), 0.0) for r in rows)
    total = fixed_usd + gap_usd
    return {
        "fixed_slippage_usd": round(fixed_usd, 4),
        "gap_through_usd": round(gap_usd, 4),
        "gap_through_rows": gap_rows,
        "slippage_total_usd": round(total, 4),
        "total_pnl_usd": round(sum(_f(r.get("realized_pnl_usd")) for r in rows), 4),
        "gross_loss_usd": round(losses, 4),
        "slippage_of_gross_loss_pct": round(
            total / abs(losses) * 100.0, 1) if losses else None,
    }


def turnover(closes_rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Annualised closes/day over the trailing ``days`` window (or whole)."""
    if not closes_rows:
        return {"n": 0, "per_day": 0.0, "per_year": 0.0}
    ordered = sorted(closes_rows, key=lambda r: _f(r.get("ts")))
    t0 = _f(ordered[0].get("ts"))
    t1 = _f(ordered[-1].get("ts"))
    span_days = (t1 - t0) / 86400000.0
    n = len(ordered)
    if days and span_days > days:
        # Only keep rows in the trailing window.
        cutoff = t1 - days * 86400000.0
        windowed = [r for r in ordered if _f(r.get("ts")) >= cutoff]
        n = len(windowed)
        span_days = days
    per_day = n / span_days if span_days > 0 else 0.0
    return {
        "n": n,
        "window_days": round(span_days, 1),
        "per_day": round(per_day, 3),
        "per_year": round(per_day * 365, 0),
    }


def concurrency(closes_rows: list[dict[str, Any]],
                opens: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct open-position count from open/close timeline."""
    events = []
    for o in opens:
        events.append((_f(o.get("ts")), 1))
    for c in closes_rows:
        events.append((_f(c.get("ts")), -1))
    events.sort(key=lambda e: e[0])
    cur = 0
    max_conc = 0
    total = 0.0
    weighted = 0.0
    prev_ts = None
    prev_cur = 0
    for ts, delta in events:
        if prev_ts is not None and ts > prev_ts:
            total += (ts - prev_ts)
            weighted += (ts - prev_ts) * prev_cur
        cur += delta
        if cur > max_conc:
            max_conc = cur
        prev_ts = ts
        prev_cur = cur
    avg = weighted / total if total > 0 else 0.0
    return {"avg_open": round(avg, 3), "max_open": max_conc}


def net_exposure_series(closes_rows: list[dict[str, Any]],
                        opens: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean/max of signed (long - short) notional over the open timeline.

    Sampled at each open/close event using the then-current open book. No beta
    (system has no per-coin beta), so this is raw signed notional / equity.
    """
    # Build per-open position notional keyed by id.
    pos_notional: dict[str, float] = {}
    pos_side: dict[str, int] = {}
    for o in opens:
        pid = o.get("position_id") or o.get("id")
        pos_notional[pid] = _f(o.get("notional_usd"))
        pos_side[pid] = 1 if o.get("side") == "long" else -1
    events = []
    for o in opens:
        events.append((_f(o.get("ts")), o.get("position_id") or o.get("id"), 1))
    for c in closes_rows:
        events.append((_f(c.get("ts")), c.get("position_id"), -1))
    events.sort(key=lambda e: e[0])
    samples = []
    net = 0.0
    for ts, pid, delta in events:
        if pid is not None:
            net += delta * pos_side.get(pid, 0) * pos_notional.get(pid, 0.0)
        samples.append(net)
    if not samples:
        return {"mean_net_usd": 0.0, "max_abs_net_usd": 0.0, "n_samples": 0}
    mean = sum(samples) / len(samples)
    max_abs = max(abs(s) for s in samples)
    return {"mean_net_usd": round(mean, 2), "max_abs_net_usd": round(max_abs, 2),
            "n_samples": len(samples)}


# --------------------------------------------------------------------------- report

def _fmt_group(stats: dict[str, Any]) -> str:
    pf = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "-"
    wr = f"{stats['win_rate']:.0f}%" if stats["win_rate"] is not None else "-"
    return (f"n={stats['n']}  win={wr}  mean={stats['mean_pnl_usd']:+.4f}$  "
            f"total={stats['total_pnl_usd']:+.4f}$  PF={pf}  "
            f"worst={stats['worst_loss_usd']:.2f}$")


def print_report(book: dict[str, Any]) -> None:
    closes_rows = closes(book)
    opens = [f for f in book["accounts"]["taker"]["fills"] if f.get("type") == "open"]
    total_pnl = sum(_f(c.get("realized_pnl_usd")) for c in closes_rows)

    print("=" * 78)
    print("Hermes shadow-book BASELINE (2026-10-16 problem list #1/#2/#6)")
    print("=" * 78)
    print(f"ledger closes        : {len(closes_rows)}  (all-time realised "
          f"pnl {total_pnl:+.4f}$)")
    print()

    print("--- reason mix (all-time) ---")
    for grp, cnt in Counter(_reason_group(c.get("reason", "")) for c in closes_rows).most_common():
        print(f"  {grp:<18} {cnt}")
    print()

    print("--- stop-loss slippage (#1) ---")
    maxloss = [c for c in closes_rows if _reason_group(c.get("reason", "")) == "max_loss"]
    print(f"  max_loss exits: {len(maxloss)}  group stats:")
    print("    " + _fmt_group(group_stats(maxloss)))
    if maxloss:
        sa = slippage_analysis(maxloss)
        print(f"  fixed slippage ${sa['fixed_slippage_usd']:.4f}  "
              f"gap-through ${sa['gap_through_usd']:.4f} "
              f"({sa['gap_through_rows']} rows)  "
              f"slippage total ${sa['slippage_total_usd']:.4f}")
        print(f"  slippage as % of max_loss gross loss: "
              f"{sa['slippage_of_gross_loss_pct']}%")
        print(f"  (all-trade fixed slippage ${sa['fixed_slippage_usd']:.4f})")
    print()

    print("--- turnover (#2) ---")
    for label, days in (("all-time", None), ("last 30d", 30), ("last 15d", 15)):
        t = turnover(closes_rows, days)
        print(f"  {label:<10} n={t['n']:>4}  span={t['window_days']:>6.1f}d  "
              f"{t['per_day']:.2f}/day  ~{t['per_year']:.0f}/yr")
    print()

    print("--- concurrency (#6) ---")
    conc = concurrency(closes_rows, opens)
    print(f"  avg open positions: {conc['avg_open']}   max: {conc['max_open']}")
    ne = net_exposure_series(closes_rows, opens)
    print(f"  signed net exposure: mean {ne['mean_net_usd']}$  "
          f"max |net| {ne['max_abs_net_usd']}$  (no beta in system)")
    print()

    print("--- overall ---")
    print("  " + _fmt_group(group_stats(closes_rows)))
    print()


# --------------------------------------------------------------------------- json

def _to_json(book: dict[str, Any]) -> dict[str, Any]:
    closes_rows = closes(book)
    opens = [f for f in book["accounts"]["taker"]["fills"] if f.get("type") == "open"]
    maxloss = [c for c in closes_rows if _reason_group(c.get("reason", "")) == "max_loss"]
    out = {
        "generated_at": _now_ms(),
        "closes": len(closes_rows),
        "overall": group_stats(closes_rows),
        "reason_mix": dict(Counter(_reason_group(c.get("reason", "")) for c in closes_rows)),
        "max_loss": group_stats(maxloss),
        "slippage": slippage_analysis(maxloss) if maxloss else {},
        "turnover": {
            "all_time": turnover(closes_rows, None),
            "last_30d": turnover(closes_rows, 30),
            "last_15d": turnover(closes_rows, 15),
        },
        "concurrency": concurrency(closes_rows, opens),
        "net_exposure": net_exposure_series(closes_rows, opens),
    }
    return out


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write machine-readable report to this path")
    args = ap.parse_args()
    book = load_book()
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(_to_json(book), fh, indent=2, ensure_ascii=False)
        print(f"wrote {args.json}")
        return
    print_report(book)


if __name__ == "__main__":
    main()
