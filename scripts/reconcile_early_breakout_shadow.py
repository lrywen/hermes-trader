#!/usr/bin/env python3
"""Reconcile the early-breakout-entry SHADOW: would an early half-size lane on
the FIRST fresh volume impulse beat waiting for structure confirmation?

Reads ~/.hermes-trading/early_breakout_shadow.jsonl (override with
HERMES_EARLY_BREAKOUT_SHADOW_FILE / --file). Each row is a LONG fresh impulse
(breakout or volume+burst) that the strict runner gate rejected because
composite/confidence were still low — the NEAR 2026-09-11 12:30 pattern. The
grader simulates the counter-factual EARLY lane:

  * enter at the signal-bar open (or logged entry_px),
  * hard stop = entry * (1 - early_stop_atr_mult * atr4h_pct/100)   (tight),
  * once price runs +protect, ratchet a trailing floor (DSL two-phase long),
  * size = early_size_fraction (0.5) of a normal unit — the reported pnl is the
    lane's per-trade spot return already scaled by that fraction so it compares
    directly against a full-size late entry.

It also records, where available, the coin's REAL move over the next window
(max favourable excursion) to show how much first-leg edge the strict gate
surrendered. Real same-side memory closes within JOIN_WINDOW_MS are preferred.

Dry-run by default; --write stamps outcomes back. Paper only; never orders.
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
    "HERMES_EARLY_BREAKOUT_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/early_breakout_shadow.jsonl"),
)
MEMORY_FILE = os.environ.get(
    "HERMES_MEMORY_FILE",
    os.environ.get("HERMES_AGENT_MEMORY_FILE",
                   str(_REPO / ".agent-memory.json")),
)
ROUND_TRIP_FEE_BPS = 5.0
SIM_BARS = 120                 # ~5 trading days on 1h
JOIN_WINDOW_MS = 8 * 3600_000
DEFAULT_PROTECT_PCT = 1.5
DEFAULT_RETRACE = 0.5


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _find_entry_bar(candles, after_ts: datetime) -> int:
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct and datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc) >= after_ts:
            return i
    return -1


def _simulate_long(entry_px, idx, candles, stop_loss_pct, protect_pct,
                   retrace_pct, max_bars=SIM_BARS) -> Tuple[float, str, float]:
    """Two-phase long walk. Returns (exit_px, reason, max_favourable_pct)."""
    peak = entry_px
    end = min(idx + 1 + max_bars, len(candles))
    for j in range(idx + 1, end):
        bar = candles[j]
        stop_px = entry_px * (1 - stop_loss_pct / 100)
        if bar.l <= stop_px:
            return min(stop_px, bar.o), "tight_stop", (peak / entry_px - 1) * 100
        if (peak - entry_px) / entry_px * 100 >= protect_pct:
            floor = entry_px + (peak - entry_px) * (1 - retrace_pct)
            if bar.l <= floor:
                return min(floor, bar.o), "trailing_stop", (peak / entry_px - 1) * 100
        if bar.h > peak:
            peak = bar.h
    return candles[end - 1].c, "window_end", (peak / entry_px - 1) * 100


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


def _join_real_long(coin, signal_ms, closes_by_coin) -> Optional[dict]:
    best = None
    for c in closes_by_coin.get(coin, []):
        if str(c.get("side") or "").lower() not in ("", "long"):
            continue
        dt = abs(float(c["closed_at"]) - signal_ms)
        if dt <= JOIN_WINDOW_MS and (best is None or dt < best[0]):
            best = (dt, c)
    return best[1] if best else None


def grade_record(r: dict, dsl_cfg: dict, closes_by_coin) -> bool:
    detail = r.get("detail") or {}
    after_ts = _parse_iso(r.get("timestamp", ""))
    if after_ts is None:
        r["outcome"] = "bad_timestamp"
        return True
    coin = r.get("coin") or ""
    signal_ms = after_ts.timestamp() * 1000.0
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0 * 100.0
    size_frac = float(detail.get("early_size_fraction", 0.5))
    r.setdefault("graded_at", datetime.now(timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))

    real = _join_real_long(coin, signal_ms, closes_by_coin)
    if real is not None and isinstance(real.get("spot_pct"), (int, float)):
        pnl = (float(real["spot_pct"]) - fee_pct) * size_frac
        r["real_close"] = {"closed_at": real.get("closed_at"),
                           "spot_pct": round(float(real["spot_pct"]), 4)}
        r["early_pnl_pct"] = round(pnl, 4)
        r["mfe_pct"] = round(float(real.get("mfe_pct") or 0.0), 3)
        r["outcome"] = "real_winner" if pnl > 0 else "real_loser"
        return True

    entry_px = detail.get("entry_px")
    atr_pct = detail.get("atr4h_pct")
    if not isinstance(atr_pct, (int, float)) or atr_pct <= 0:
        r["outcome"] = "no_atr"
        return True
    try:
        candles = fetch_hl_candles(coin, "1h", 300)
    except Exception as e:
        r["sim_error"] = f"fetch: {e}"
        return False
    idx = _find_entry_bar(candles, after_ts)
    if idx < 0 or idx >= len(candles) - 2:
        r["outcome"] = "no_future_bars"
        return True
    if not isinstance(entry_px, (int, float)) or entry_px <= 0:
        entry_px = float(candles[idx].o)
        r["entry_px_source"] = "signal_bar_open"

    stop_mult = float(detail.get("early_stop_atr_mult", 1.2))
    stop_loss_pct = float(atr_pct) * stop_mult
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg,
                            default=DEFAULT_PROTECT_PCT))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg,
                            default=DEFAULT_RETRACE))
    exit_px, reason, mfe = _simulate_long(
        float(entry_px), idx, candles, stop_loss_pct, protect, retrace)
    gross = (exit_px / float(entry_px) - 1) * 100 - fee_pct
    r["exit_px"] = round(exit_px, 8)
    r["exit_reason"] = reason
    r["stop_loss_pct"] = round(stop_loss_pct, 3)
    r["mfe_pct"] = round(mfe, 3)
    r["early_pnl_pct"] = round(gross * size_frac, 4)   # half-size lane
    r["full_size_pnl_pct"] = round(gross, 4)
    r["outcome"] = "sim_winner" if gross * size_frac > 0 else "sim_loser"
    return True


def _stats(rows, key):
    vals = [float(r[key]) for r in rows if key in r]
    if not vals:
        return None
    wins = [v for v in vals if v > 0]
    return {"n": len(vals), "win": round(100 * len(wins) / len(vals), 1),
            "mean": round(sum(vals) / len(vals), 3),
            "sum": round(sum(vals), 3)}


def _summary(records: List[dict]) -> None:
    useful = [r for r in records if r.get("outcome") not in (None, "")
              and "early_pnl_pct" in r]
    print("\n=== early-breakout-entry shadow reconciliation ===")
    print(f"records={len(records)}  graded={len(useful)}")
    e = _stats(useful, "early_pnl_pct")
    f = _stats(useful, "full_size_pnl_pct")
    if e:
        print(f"\n  EARLY half-size lane : n={e['n']:3d} win={e['win']:5.1f}% "
              f"mean={e['mean']:+.3f}% sum={e['sum']:+.2f}%")
    if f:
        print(f"  (same signals full sz): n={f['n']:3d} win={f['win']:5.1f}% "
              f"mean={f['mean']:+.3f}%")
    # exit-reason and MFE surrendered
    reasons: Dict[str, int] = {}
    mfe = [float(r["mfe_pct"]) for r in useful if r.get("mfe_pct") is not None]
    for r in useful:
        reasons[r.get("exit_reason", "?")] = reasons.get(r.get("exit_reason", "?"), 0) + 1
    if mfe:
        print(f"  first-leg MFE: mean={sum(mfe)/len(mfe):+.2f}% "
              f"max={max(mfe):+.2f}% (edge the strict gate leaves on table)")
    print("  exit reasons:", reasons)

    print("\n  per-record:")
    for r in sorted(useful, key=lambda x: x.get("timestamp", "")):
        d = r.get("detail") or {}
        src = "real" if r.get("real_close") else "sim"
        print(f"    {r.get('timestamp')}  {str(r.get('coin')):9} "
              f"score={d.get('composite_score')} fresh={int(bool(d.get('fresh_impulse')))} "
              f"early={r.get('early_pnl_pct'):+.2f}% mfe={r.get('mfe_pct')} "
              f"[{src}/{r.get('exit_reason','')}]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--window-hours", type=float, default=1.0)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"shadow file not found (no early-breakout candidate yet): {args.file}")
        return 1
    records = [json.loads(l) for l in open(args.file, encoding="utf-8") if l.strip()]
    cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    cfg = read_agent_config() or {}
    closes = _load_real_closes()
    graded = pending = 0
    for r in records:
        if r.get("outcome") is not None:
            continue
        ts = _parse_iso(r.get("timestamp", ""))
        if ts is None or ts.timestamp() > cutoff:
            pending += 1
            continue
        pending += 0 if grade_record(r, cfg, closes) else 1
        graded += 1 if r.get("outcome") else 0
    print(f"total={len(records)}  newly_graded={graded}  pending={pending}")
    _summary(records)
    if args.write:
        with open(args.file, "w", encoding="utf-8") as fp:
            for r in records:
                fp.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nOutcomes written back to {args.file}")
    else:
        print("\n(dry-run; pass --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
