#!/usr/bin/env python3
"""Reconcile the per-coin macro×own regime SHADOW with would-be outcomes.

Reads ~/.hermes-trading/per_coin_regime_shadow.jsonl (override with
HERMES_PER_COIN_REGIME_SHADOW_FILE / --file). Each row is a SCAN-TIME candidate
where the BTC macro gate said "aligned"; the probe also classified the coin's
OWN 1h into a quadrant tier:

    strong       macro aligned AND own 1h aligned AND own score >= strong
    mid          macro aligned AND own not against (neutral/chop/weak-align)
    weak_review  macro aligned BUT own 1h points AGAINST the side (ZEC case)

The decision this reconciliation informs is whether to ENFORCE the tier
structure instead of a single global min_trend_score. So the headline is the
counter-factual expectancy PER TIER: if weak_review candidates actually fade
and strong candidates follow through, the tier split has discriminating power.

Outcome source per record (no entry_px is logged — these are scan points):
  1. a REAL memory close of the same coin within JOIN_WINDOW_MS (preferred —
     these are actual trades), using its spot_pct in the trade direction;
  2. otherwise a simplified two-phase DSL candle walk from the signal bar open
     in the trade direction (long AND short), mirroring reconcile_risk_tuning.

Additionally the quick own-4h soft-demotion flag (would="demote_to_weak_aligned")
is tallied on its own: of the aligned candidates that rule would demote, how
many were winners/losers (the false-demote cost).

Pure paper reconciliation: never places orders. Dry-run by default; --write
stamps outcome fields back into the JSONL.

Usage:
    python3 scripts/reconcile_per_coin_regime_shadow.py
    python3 scripts/reconcile_per_coin_regime_shadow.py --window-hours 24 --write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ["HERMES_BACKTEST"] = "1"
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config_store import cfg_get, read_agent_config  # noqa
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402

SHADOW_FILE = os.environ.get(
    "HERMES_PER_COIN_REGIME_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/per_coin_regime_shadow.jsonl"),
)
MEMORY_FILE = os.environ.get(
    "HERMES_MEMORY_FILE",
    os.environ.get("HERMES_AGENT_MEMORY_FILE",
                   str(_REPO / ".agent-memory.json")),
)
ROUND_TRIP_FEE_BPS = 5.0
SIM_BARS = 180                       # max 1h bars walked per signal
JOIN_WINDOW_MS = 6 * 3600_000        # match a real close within 6h
TIERS = ("strong", "mid", "weak_review")
_TERMINAL = {"bad_timestamp", "no_side", "no_future_bars", "fetch_error",
             "tier_na"}


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _find_entry_bar(candles, after_ts: datetime) -> int:
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        if datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc) >= after_ts:
            return i
    return -1


def _simulate_side(entry_px: float, entry_idx: int, candles, side: str,
                   max_loss_pct: float, protect_pct: float,
                   retrace_pct: float, max_bars: int = SIM_BARS
                   ) -> Tuple[float, str, int]:
    """Two-phase DSL walk in the trade direction. Long mirrors the risk-tuning
    reconcile; short is the mirrored image (stop above, trailing on a trough)."""
    long = side != "short"
    extreme = entry_px          # peak for long, trough for short

    def fav(px: float) -> float:
        # favorable excursion % in the trade direction
        return (px - entry_px) / entry_px * 100 if long \
            else (entry_px - px) / entry_px * 100

    end = min(entry_idx + 1 + max_bars, len(candles))
    for j in range(entry_idx + 1, end):
        bar = candles[j]
        if long:
            stop_px = entry_px * (1 - max_loss_pct / 100)
            hit_stop = bar.l <= stop_px
            fill = min(stop_px, bar.o)
            new_extreme = bar.h
            improved = new_extreme > extreme
            floor = entry_px + (extreme - entry_px) * (1 - retrace_pct)
            trail_hit = bar.l <= floor
        else:
            stop_px = entry_px * (1 + max_loss_pct / 100)
            hit_stop = bar.h >= stop_px
            fill = max(stop_px, bar.o)
            new_extreme = bar.l
            improved = new_extreme < extreme
            floor = entry_px - (entry_px - extreme) * (1 - retrace_pct)
            trail_hit = bar.h >= floor
        if hit_stop:
            return fill, f"max_loss {max_loss_pct:g}%", j
        if fav(extreme) >= protect_pct and trail_hit:
            return (min(floor, bar.o) if long else max(floor, bar.o)), \
                "trailing_stop", j
        if improved:
            extreme = new_extreme
    last = candles[end - 1]
    return last.c, "window_end", end - 1


def _load_real_closes() -> Dict[str, list]:
    try:
        mem = json.load(open(MEMORY_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, list] = {}
    for c in mem.get("closes", []) or []:
        coin = str(c.get("coin") or "")
        if coin and isinstance(c.get("closed_at"), (int, float)):
            out.setdefault(coin, []).append(c)
    return out


def _join_real_close(coin: str, signal_ms: float,
                     closes_by_coin: Dict[str, list], side: str
                     ) -> Optional[dict]:
    """Nearest real close in the window whose side matches the candidate."""
    best = None
    want = side.lower()
    for c in closes_by_coin.get(coin, []):
        if str(c.get("side") or "").lower() not in ("", want):
            continue
        dt = abs(float(c["closed_at"]) - signal_ms)
        if dt <= JOIN_WINDOW_MS and (best is None or dt < best[0]):
            best = (dt, c)
    return best[1] if best else None


def grade_record(r: dict, dsl_cfg: dict, closes_by_coin: Dict[str, list]) -> bool:
    """Fill outcome fields on one row. False => not gradeable yet (leave pending)."""
    detail = r.get("detail") or {}
    tier = detail.get("quadrant_tier")
    if tier not in TIERS:
        r["outcome"] = "tier_na"
        return True

    after_ts = _parse_iso(r.get("timestamp", ""))
    if after_ts is None:
        r["outcome"] = "bad_timestamp"
        return True
    side = str(r.get("side") or "").lower()
    if side not in ("long", "short"):
        r["outcome"] = "no_side"
        return True
    coin = r.get("coin") or ""
    signal_ms = after_ts.timestamp() * 1000.0
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0 * 100.0
    r.setdefault("graded_at", datetime.now(timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))

    # 1) real close (actual trade) preferred
    real = _join_real_close(coin, signal_ms, closes_by_coin, side)
    if real is not None and isinstance(real.get("spot_pct"), (int, float)):
        spot = float(real["spot_pct"])
        signed = spot if side == "long" else -spot
        pnl = signed - fee_pct
        r["real_close"] = {"closed_at": real.get("closed_at"),
                           "spot_pct": round(spot, 4),
                           "realized_pnl_usd": real.get("realized_pnl_usd")}
        r["pnl_pct"] = round(pnl, 4)
        r["outcome"] = "real_winner" if pnl > 0 else "real_loser"
        return True

    # 2) candle walk from the signal bar open
    try:
        candles = fetch_hl_candles(coin, "1h", 300)
    except Exception as e:  # network: retry next run
        r["sim_error"] = f"fetch: {e}"
        return False
    idx = _find_entry_bar(candles, after_ts)
    if idx < 0 or idx >= len(candles) - 2:
        r["outcome"] = "no_future_bars"
        return True
    entry_px = float(candles[idx].o)
    if entry_px <= 0:
        r["outcome"] = "no_future_bars"
        return True

    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg))
    exit_px, reason, _ = _simulate_side(entry_px, idx, candles, side,
                                        max_loss, protect, retrace)
    sign = 1.0 if side == "long" else -1.0
    pnl = ((exit_px - entry_px) / entry_px * 100 * sign) - fee_pct
    r["entry_px"] = round(entry_px, 8)
    r["exit_px"] = round(exit_px, 8)
    r["exit_reason"] = reason
    r["pnl_pct"] = round(pnl, 4)
    r["outcome"] = "sim_winner" if pnl > 0 else "sim_loser"
    return True


def _stats(rows: List[dict]) -> Dict[str, Any]:
    pnls = [float(r["pnl_pct"]) for r in rows if "pnl_pct" in r]
    if not pnls:
        return {"n": len(rows), "graded": 0}
    wins = [p for p in pnls if p > 0]
    return {
        "n": len(rows), "graded": len(pnls),
        "winners": len(wins), "losers": len(pnls) - len(wins),
        "win_rate": round(100 * len(wins) / len(pnls), 1),
        "mean_pct": round(sum(pnls) / len(pnls), 3),
        "sum_pct": round(sum(pnls), 3),
    }


def _summary(records: List[dict]) -> None:
    graded = [r for r in records if r.get("outcome") not in (None, "")]
    useful = [r for r in graded if "pnl_pct" in r]

    print("\n=== per-coin regime shadow reconciliation ===")
    print(f"records={len(records)}  graded={len(graded)}  "
          f"with_pnl={len(useful)}")

    # Headline: counter-factual EV per quadrant tier.
    print("\n── counter-factual expectancy by quadrant tier ──")
    by_tier: Dict[str, List[dict]] = {t: [] for t in TIERS}
    for r in useful:
        t = (r.get("detail") or {}).get("quadrant_tier")
        if t in by_tier:
            by_tier[t].append(r)
    for t in TIERS:
        s = _stats(by_tier[t])
        if s.get("graded"):
            print(f"  {t:12} n={s['graded']:3d}  win={s['win_rate']:5.1f}%  "
                  f"mean={s['mean_pct']:+.3f}%  sum={s['sum_pct']:+.2f}%  "
                  f"(W{s['winners']}/L{s['losers']})")
        else:
            print(f"  {t:12} n=  0  (no graded samples yet)")

    # Quick own-4h soft-demotion tally.
    print("\n── own-4h soft-demotion (would=demote_to_weak_aligned) ──")
    dem = [r for r in useful if r.get("would") == "demote_to_weak_aligned"]
    free = [r for r in useful if r.get("would") == "pass"]
    for label, rows in (("would demote", dem), ("would pass", free)):
        s = _stats(rows)
        if s.get("graded"):
            print(f"  {label:12} n={s['graded']:3d}  win={s['win_rate']:5.1f}%  "
                  f"mean={s['mean_pct']:+.3f}%  sum={s['sum_pct']:+.2f}%")
        else:
            print(f"  {label:12} n=  0")

    # Outcome-source + terminal buckets.
    print("\n── outcome buckets ──")
    counts: Dict[str, int] = {}
    for r in graded:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    for k in sorted(counts):
        print(f"  {k:16} {counts[k]}")

    print("\n  per-record detail:")
    for r in sorted(records, key=lambda x: x.get("timestamp", "")):
        if r.get("outcome") in (None, "") or r["outcome"] in _TERMINAL:
            continue
        d = r.get("detail") or {}
        src = "real" if r.get("real_close") else "sim"
        pnl = r.get("pnl_pct")
        pnl_s = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "n/a"
        print(f"    {r.get('timestamp')}  {str(r.get('coin')):9} "
              f"{str(r.get('side')):5} "
              f"tier={str(d.get('quadrant_tier')):11} "
              f"own1h={str(d.get('own_1h_regime')):7} "
              f"pnl={pnl_s} [{src}/{r.get('exit_reason','')}]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--window-hours", type=float, default=24.0,
                    help="Only grade records older than this many hours")
    ap.add_argument("--write", action="store_true",
                    help="Write outcomes back to the JSONL (default: report)")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"shadow file not found (no candidate recorded yet): {args.file}")
        return 1

    records = [json.loads(line) for line in open(args.file, encoding="utf-8")
               if line.strip()]
    cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    dsl_cfg = read_agent_config() or {}
    closes_by_coin = _load_real_closes()

    graded = pending = 0
    for r in records:
        if r.get("outcome") is not None:
            continue
        ts = _parse_iso(r.get("timestamp", ""))
        if ts is None or ts.timestamp() > cutoff:
            pending += 1
            continue
        if grade_record(r, dsl_cfg, closes_by_coin):
            graded += 1
        else:
            pending += 1

    print(f"total={len(records)}  newly_graded={graded}  "
          f"pending(immature/unmatched)={pending}")
    _summary(records)

    if args.write:
        with open(args.file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nOutcomes written back to {args.file}")
    else:
        print("\n(dry-run; pass --write to persist outcomes into the JSONL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
