#!/usr/bin/env python3
"""Reconcile the short-only SHADOW: would re-enabling shorts have been +EV?

Reads ~/.hermes-trading/short_only_shadow.jsonl (override with
HERMES_SHORT_ONLY_SHADOW_FILE / --file). Every row is a SHORT candidate the
operator's runner_entry_gate.allow_shorts=false switch suppressed. The offline
walk grades what each short WOULD have returned and — crucially — separates the
signals that re-enabling shorts would actually admit from the noise:

  Tier A (admittable): clears the SAME post-switch gates a live short must pass
                       after the allow_shorts flip:
                         confidence >= min_short_confidence (default 0.68) AND
                         structured_short: downtrend flag, OR fresh impulse +
                         composite >= min_short_composite (default 40).
  Tier B (would-still-block): fails confidence or structure even with shorts on.

Only Tier A is relevant to the enable decision. Within Tier A it further
slices by macro (BTC proxy) vs the coin's OWN 1h regime, surfacing the BTC-proxy
mismatch on the short side (macro down + own down = highest conviction).

Outcome source:
  1. a REAL same-side memory close of the coin within JOIN_WINDOW_MS (actual
     trade, spot flipped for short); else
  2. a direction-aware two-phase DSL 1h-candle walk from the signal bar open.

Dry-run by default; --write stamps outcomes back into the JSONL. Read-only /
paper: never places orders.

Usage:
    python3 scripts/reconcile_short_only_shadow.py
    python3 scripts/reconcile_short_only_shadow.py --window-hours 24 --write
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
    "HERMES_SHORT_ONLY_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/short_only_shadow.jsonl"),
)
MEMORY_FILE = os.environ.get(
    "HERMES_MEMORY_FILE",
    os.environ.get("HERMES_AGENT_MEMORY_FILE",
                   str(_REPO / ".agent-memory.json")),
)
ROUND_TRIP_FEE_BPS = 5.0
SIM_BARS = 180
JOIN_WINDOW_MS = 6 * 3600_000
_TERMINAL = {"bad_timestamp", "no_entry_px", "no_future_bars", "fetch_error"}


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
        if ct and datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc) >= after_ts:
            return i
    return -1


def _simulate_short(entry_px: float, entry_idx: int, candles,
                    max_loss_pct: float, protect_pct: float,
                    retrace_pct: float, max_bars: int = SIM_BARS
                    ) -> Tuple[float, str]:
    """Two-phase DSL walk for a SHORT: hard stop above, trailing floor ratchets
    DOWN off a trough once profit >= protect. Mirrors dsl_exit short handling."""
    trough = entry_px
    end = min(entry_idx + 1 + max_bars, len(candles))
    for j in range(entry_idx + 1, end):
        bar = candles[j]
        stop_px = entry_px * (1 + max_loss_pct / 100)
        if bar.h >= stop_px:
            return max(stop_px, bar.o), f"max_loss {max_loss_pct:g}%"
        if (entry_px - trough) / entry_px * 100 >= protect_pct:
            floor = entry_px - (entry_px - trough) * (1 - retrace_pct)
            if bar.h >= floor:
                return max(floor, bar.o), "trailing_stop"
        if bar.l < trough:
            trough = bar.l
    return candles[end - 1].c, "window_end"


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


def _join_real_short_close(coin: str, signal_ms: float,
                           closes_by_coin: Dict[str, list]) -> Optional[dict]:
    best = None
    for c in closes_by_coin.get(coin, []):
        if str(c.get("side") or "").lower() not in ("", "short"):
            continue
        dt = abs(float(c["closed_at"]) - signal_ms)
        if dt <= JOIN_WINDOW_MS and (best is None or dt < best[0]):
            best = (dt, c)
    return best[1] if best else None


def _admittable(detail: dict, cfg: dict) -> Tuple[bool, dict]:
    """Would this short clear the post-flip runner gates (Tier A)?"""
    gate = cfg.get("runner_entry_gate") or {}
    min_conf = float(gate.get("min_short_confidence", 0.68))
    min_comp = float(gate.get("min_short_composite", 40))
    conf = detail.get("confidence")
    score = detail.get("composite_score")
    downtrend = bool(detail.get("downtrend"))
    fresh = bool(detail.get("breakout") or (detail.get("volume_spike")
                                            and detail.get("burst"))
                  or (detail.get("burst") and (score or 0) >= min_comp))
    slow = int(detail.get("slow_burn_count") or 0)
    conf_ok = conf is not None and float(conf) >= min_conf
    structured = downtrend or (
        (score is not None and float(score) >= min_comp and (slow >= 1 or fresh))
    ) or (fresh and (score is not None and float(score) >= min_comp))
    return (conf_ok and structured), {
        "min_short_confidence": min_conf, "min_short_composite": min_comp,
        "conf_ok": conf_ok, "structured": structured, "fresh": fresh}


def grade_record(r: dict, dsl_cfg: dict, cfg: dict,
                 closes_by_coin: Dict[str, list]) -> bool:
    detail = r.get("detail") or {}
    after_ts = _parse_iso(r.get("timestamp", ""))
    if after_ts is None:
        r["outcome"] = "bad_timestamp"
        return True
    coin = r.get("coin") or ""
    signal_ms = after_ts.timestamp() * 1000.0
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0 * 100.0
    r.setdefault("graded_at", datetime.now(timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))

    admittable, adm_detail = _admittable(detail, cfg)
    r["admittable_if_enabled"] = admittable
    r["admit_check"] = adm_detail

    real = _join_real_short_close(coin, signal_ms, closes_by_coin)
    if real is not None and isinstance(real.get("spot_pct"), (int, float)):
        # real short: positive pnl when price fell -> -spot for the short side
        pnl = -float(real["spot_pct"]) - fee_pct
        r["real_close"] = {"closed_at": real.get("closed_at"),
                           "spot_pct": round(float(real["spot_pct"]), 4)}
        r["pnl_pct"] = round(pnl, 4)
        r["outcome"] = "real_winner" if pnl > 0 else "real_loser"
        return True

    entry_px = detail.get("entry_px")
    if not isinstance(entry_px, (int, float)) or entry_px <= 0:
        entry_px = None      # fall back to the signal-bar open below
    try:
        candles = fetch_hl_candles(coin, "1h", 300)
    except Exception as e:
        r["sim_error"] = f"fetch: {e}"
        return False
    idx = _find_entry_bar(candles, after_ts)
    if idx < 0 or idx >= len(candles) - 2:
        r["outcome"] = "no_future_bars"
        return True
    if entry_px is None:
        # legacy/scan records logged no price: enter at the signal bar open
        entry_px = float(candles[idx].o)
        r["entry_px_source"] = "signal_bar_open"
    if entry_px <= 0:
        r["outcome"] = "no_entry_px"
        return True

    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg))
    exit_px, reason = _simulate_short(float(entry_px), idx, candles,
                                      max_loss, protect, retrace)
    pnl = ((float(entry_px) - exit_px) / float(entry_px) * 100) - fee_pct
    r["exit_px"] = round(exit_px, 8)
    r["exit_reason"] = reason
    r["pnl_pct"] = round(pnl, 4)
    r["outcome"] = "sim_winner" if pnl > 0 else "sim_loser"
    return True


def _stats(rows: List[dict]) -> Dict[str, Any]:
    pnls = [float(r["pnl_pct"]) for r in rows if "pnl_pct" in r]
    if not pnls:
        return {"graded": 0}
    wins = [p for p in pnls if p > 0]
    return {"graded": len(pnls), "winners": len(wins),
            "losers": len(pnls) - len(wins),
            "win_rate": round(100 * len(wins) / len(pnls), 1),
            "mean_pct": round(sum(pnls) / len(pnls), 3),
            "sum_pct": round(sum(pnls), 3)}


def _print_block(title: str, rows: List[dict]) -> None:
    s = _stats(rows)
    if not s.get("graded"):
        print(f"  {title:34} n=  0")
        return
    print(f"  {title:34} n={s['graded']:3d}  win={s['win_rate']:5.1f}%  "
          f"mean={s['mean_pct']:+.3f}%  sum={s['sum_pct']:+.2f}%  "
          f"(W{s['winners']}/L{s['losers']})")


def _summary(records: List[dict]) -> None:
    useful = [r for r in records
              if r.get("outcome") not in (None, "") and "pnl_pct" in r]
    print("\n=== short-only shadow reconciliation ===")
    print(f"records={len(records)}  graded_with_pnl={len(useful)}")

    tier_a = [r for r in useful if r.get("admittable_if_enabled")]
    tier_b = [r for r in useful if not r.get("admittable_if_enabled")]
    print("\n── decision tiers (post allow_shorts flip) ──")
    _print_block("ALL suppressed shorts", useful)
    _print_block("Tier A admittable (conf+struct)", tier_a)
    _print_block("Tier B still blocked", tier_b)

    # Tier A by macro × own-1h regime (BTC-proxy mismatch audit)
    print("\n── Tier A by macro(BTC) × coin own-1h regime ──")
    groups: Dict[str, list] = {}
    for r in tier_a:
        d = r.get("detail") or {}
        key = f"macro={str(d.get('macro_regime')):7} own1h={str(d.get('own_1h_regime')):7}"
        groups.setdefault(key, []).append(r)
    if groups:
        for k in sorted(groups):
            _print_block(k, groups[k])
    else:
        print("  (no admittable samples yet)")

    print("\n── outcome buckets ──")
    counts: Dict[str, int] = {}
    for r in records:
        if r.get("outcome"):
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    for k in sorted(counts):
        print(f"  {k:14} {counts[k]}")

    print("\n  Tier-A per-record detail:")
    for r in sorted(tier_a, key=lambda x: x.get("timestamp", "")):
        d = r.get("detail") or {}
        src = "real" if r.get("real_close") else "sim"
        print(f"    {r.get('timestamp')}  {str(r.get('coin')):9} "
              f"conf={d.get('confidence')} score={d.get('composite_score')} "
              f"macro={str(d.get('macro_regime')):4} "
              f"own={str(d.get('own_1h_regime')):4} "
              f"pnl={r.get('pnl_pct'):+.2f}% [{src}/{r.get('exit_reason','')}]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--window-hours", type=float, default=24.0,
                    help="Only grade records older than this many hours")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"shadow file not found (no short candidate recorded yet): {args.file}")
        return 1

    records = [json.loads(line) for line in open(args.file, encoding="utf-8")
               if line.strip()]
    cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    cfg = read_agent_config() or {}
    dsl_cfg = cfg
    closes_by_coin = _load_real_closes()

    graded = pending = 0
    for r in records:
        if r.get("outcome") is not None:
            continue
        ts = _parse_iso(r.get("timestamp", ""))
        if ts is None or ts.timestamp() > cutoff:
            pending += 1
            continue
        if grade_record(r, dsl_cfg, cfg, closes_by_coin):
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
