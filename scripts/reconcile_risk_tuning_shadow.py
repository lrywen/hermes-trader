#!/usr/bin/env python3
"""Reconcile risk-tuning SHADOW verdicts with would-be outcomes.

Reads ~/.hermes-trading/risk_tuning_shadow.jsonl (path overridable via
HERMES_RISK_TUNING_SHADOW_FILE) and grades each counter-factual rule:

  * breakout_score_floor / per_coin_cooldown (would="block"):
      The signal was a live ADMIT. Fetch 1h candles after the signal and run
      the same simplified two-phase DSL simulation used by the pullback
      reconcile, so a blocked-by-the-proposal admit is graded on what it would
      have made. Aggregate: of the trades the proposal WOULD have blocked, how
      many were actually winners/losers and the counter-factual expectancy —
      i.e. the false-block cost (a good proposal blocks mostly losers).

  * leverage_tier (would="deleverage"):
      The signal was live at the high leverage. Prefer the REAL close when the
      same coin has a memory close shortly after the signal; otherwise fall
      back to candle simulation. Compare PnL at live_leverage vs
      proposed_leverage to size the risk reduction (and the upside given up).

  * stop_tuning (would="survive_wider_cap" / "breakeven_would_arm"):
      Recorded at a real max_loss exit. Simulate the same bars with the
      candidate wider cap and the live cap: report whether the wider stop
      survives to a profit / times out at a profit, or just stops out later at
      a bigger loss — the central "noise stop vs real stop" question.

Pure paper reconciliation: never places orders. Default is a dry-run report;
pass --write to stamp outcome fields back into the JSONL.

Usage:
    python3 scripts/reconcile_risk_tuning_shadow.py
    python3 scripts/reconcile_risk_tuning_shadow.py --window-hours 24 --write
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

from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.client.hl_client import fetch_hl_candles

SHADOW_FILE = os.environ.get(
    "HERMES_RISK_TUNING_SHADOW_FILE",
    os.path.expanduser("~/.hermes-trading/risk_tuning_shadow.jsonl"),
)
MEMORY_FILE = os.environ.get(
    "HERMES_MEMORY_FILE",
    # Mirror agents/memory.py: env override (the live container sets
    # HERMES_AGENT_MEMORY_FILE=/data/.agent-memory.json), else repo-root file.
    os.environ.get(
        "HERMES_AGENT_MEMORY_FILE",
        str(_REPO / ".agent-memory.json")),
)
ROUND_TRIP_FEE_BPS = 5.0
SIM_BARS = 180          # max 1h bars walked per signal (7.5 days)
JOIN_WINDOW_MS = 6 * 3600_000   # match a real close within 6h of the signal


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _find_entry_bar(candles, after_ts: datetime) -> int:
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        if datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc) >= after_ts:
            return i
    return -1


def _simulate(entry_px: float, entry_idx: int, candles,
              max_loss_pct: float, protect_pct: float,
              retrace_pct: float,
              max_bars: int = SIM_BARS) -> Tuple[float, str, int]:
    """Long-only two-phase walk; mirror of reconcile_pullback_shadow.

    Returns (exit_px, reason, exit_idx). Shorts are marked unsupported by the
    caller (the audited 2026-09-10 set is long-only).
    """
    peak = entry_px
    end = min(entry_idx + 1 + max_bars, len(candles))
    for j in range(entry_idx + 1, end):
        bar = candles[j]
        stop_px = entry_px * (1 - max_loss_pct / 100)
        if bar.l <= stop_px:
            return min(stop_px, bar.o), f"max_loss {max_loss_pct:g}%", j
        if peak > entry_px:
            profit_pct = (peak - entry_px) / entry_px * 100
            if profit_pct >= protect_pct:
                floor = entry_px + (peak - entry_px) * (1 - retrace_pct)
                if bar.l <= floor:
                    return min(floor, bar.o), "trailing_stop", j
        if bar.h > peak:
            peak = bar.h
    last = candles[end - 1]
    return last.c, "window_end", end - 1


def _load_real_closes() -> Dict[str, list]:
    """coin -> list of {closed_at, spot_pct, realized_pnl_usd, leverage}."""
    try:
        mem = json.load(open(MEMORY_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, list] = {}
    for c in mem.get("closes", []) or []:
        coin = str(c.get("coin") or "")
        ts = c.get("closed_at")
        if not coin or not isinstance(ts, (int, float)):
            continue
        out.setdefault(coin, []).append(c)
    return out


def _join_real_close(coin: str, signal_ms: float,
                     closes_by_coin: Dict[str, list]) -> Optional[dict]:
    best = None
    for c in closes_by_coin.get(coin, []):
        dt = abs(float(c["closed_at"]) - signal_ms)
        if dt <= JOIN_WINDOW_MS and (best is None or dt < best[0]):
            best = (dt, c)
    return best[1] if best else None


def _pnl_pct_at_leverage(spot_pct: float, leverage: float) -> float:
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0 * 100.0  # round-trip fee in %
    return spot_pct * leverage - fee_pct


def grade_record(r: dict, dsl_cfg: dict, closes_by_coin: Dict[str, list]) -> bool:
    """Fill outcome fields on one record. Returns False if not gradeable yet."""
    rule = r.get("rule")
    detail = r.get("detail") or {}
    after_ts = _parse_iso(r.get("timestamp", ""))
    if after_ts is None:
        r["outcome"] = "bad_timestamp"
        return True
    coin = r.get("coin") or ""
    signal_ms = after_ts.timestamp() * 1000.0

    if rule == "stop_tuning":
        # No candles needed for the headline counter-factual flags, but a
        # candle walk with the WIDER cap tells us if it actually turns green.
        r.setdefault("graded_at", datetime.now(timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"))
        entry_px = float(detail.get("entry_px") or 0)
        cap = detail.get("candidate_max_loss_pct")
        if entry_px > 0 and cap:
            try:
                candles = fetch_hl_candles(coin, "1h", 300)
                idx = _find_entry_bar(candles, after_ts)
                if 0 <= idx < len(candles) - 2:
                    live_cap = float(detail.get("live_spot_cap_pct") or
                                     cfg_get("dsl_exit.max_loss_pct",
                                             config=dsl_cfg))
                    protect = float(cfg_get("dsl_exit.protect_pct",
                                            config=dsl_cfg))
                    retrace = float(cfg_get("dsl_exit.retrace_threshold",
                                            config=dsl_cfg))
                    wx, wr, _ = _simulate(entry_px, idx, candles, float(cap),
                                          protect, retrace)
                    lx, lr, _ = _simulate(entry_px, idx, candles, live_cap,
                                          protect, retrace)
                    fee = ROUND_TRIP_FEE_BPS / 10000.0
                    r["wider_cap_sim"] = {
                        "exit_px": round(wx, 6), "reason": wr,
                        "pnl_pct": round((wx - entry_px) / entry_px * 100
                                         - fee * 100, 3),
                    }
                    r["live_cap_sim"] = {
                        "exit_px": round(lx, 6), "reason": lr,
                        "pnl_pct": round((lx - entry_px) / entry_px * 100
                                         - fee * 100, 3),
                    }
            except Exception as e:  # network/parse: leave candle fields empty
                r["sim_error"] = str(e)
        r["outcome"] = (
            "wider_cap_would_green"
            if (r.get("wider_cap_sim") or {}).get("pnl_pct", 0) > 0
            else "wider_cap_no_gain")
        return True

    if str(r.get("side") or "").lower() not in ("", "long"):
        r["outcome"] = "short_unsupported"
        return True

    # leverage_tier: prefer the real close (these were live trades).
    if rule == "leverage_tier":
        real = _join_real_close(coin, signal_ms, closes_by_coin)
        if real and isinstance(real.get("spot_pct"), (int, float)):
            spot = float(real["spot_pct"])
            live_lev = float(detail.get("live_leverage") or
                             real.get("leverage") or 10)
            prop_lev = float(detail.get("proposed_leverage") or 5)
            r["real_close"] = {
                "closed_at": real.get("closed_at"),
                "spot_pct": round(spot, 4),
                "realized_pnl_usd": real.get("realized_pnl_usd"),
            }
            r["live_pnl_pct"] = round(_pnl_pct_at_leverage(spot, live_lev), 3)
            r["proposed_pnl_pct"] = round(
                _pnl_pct_at_leverage(spot, prop_lev), 3)
            r["outcome"] = ("deleverage_avoids_loss"
                            if r["live_pnl_pct"] < 0 else "deleverage_gives_up_gain")
            return True
        # fall through to candle simulation if no real close join
        entry_px = float(detail.get("entry_px") or 0)
        if entry_px <= 0:
            return False
    elif rule in ("breakout_score_floor", "per_coin_cooldown"):
        entry_px = float(detail.get("entry_px") or 0)
        if entry_px <= 0:
            r["outcome"] = "no_entry_px"
            return True
    else:
        return False

    # Candle simulation for block proposals and unmatched leverage signals.
    try:
        candles = fetch_hl_candles(coin, "1h", 300)
    except Exception as e:
        r["sim_error"] = f"fetch: {e}"
        return False
    idx = _find_entry_bar(candles, after_ts)
    if idx < 0 or idx >= len(candles) - 2:
        return False  # not enough future bars yet; leave pending

    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg))
    exit_px, reason, _ = _simulate(entry_px, idx, candles, max_loss,
                                   protect, retrace)
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    spot_pct = (exit_px - entry_px) / entry_px
    pnl_pct = (spot_pct - fee) * 100
    r["exit_px"] = round(exit_px, 6)
    r["exit_reason"] = reason
    if rule == "leverage_tier":
        live_lev = float(detail.get("live_leverage") or 10)
        prop_lev = float(detail.get("proposed_leverage") or 5)
        r["live_pnl_pct"] = round(pnl_pct * live_lev, 3)
        r["proposed_pnl_pct"] = round(pnl_pct * prop_lev, 3)
        r["outcome"] = ("deleverage_avoids_loss" if pnl_pct < 0
                        else "deleverage_gives_up_gain")
    else:
        r["pnl_pct"] = round(pnl_pct, 4)
        # A block proposal is "good" when the admit it would have blocked lost.
        r["outcome"] = "blocked_a_loser" if pnl_pct <= 0 else "blocked_a_winner"
    return True


def _summary(records: List[dict]) -> None:
    by_rule: Dict[str, List[dict]] = {}
    for r in records:
        if r.get("outcome") and r["outcome"] not in (
                "bad_timestamp", "no_entry_px", "short_unsupported",
                "no_future_bars"):
            by_rule.setdefault(r["rule"], []).append(r)

    print("\n=== risk-tuning shadow reconciliation ===")
    for rule, rows in sorted(by_rule.items()):
        graded = [r for r in rows if r.get("outcome") not in (None, "")]
        print(f"\n[{rule}]  graded={len(graded)} / total={len(rows)}")
        counts: Dict[str, int] = {}
        for r in graded:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        for k, v in sorted(counts.items()):
            print(f"    {k:30} {v}")

        if rule in ("breakout_score_floor", "per_coin_cooldown"):
            pnls = [float(r.get("pnl_pct", 0)) for r in graded
                    if "pnl_pct" in r]
            if pnls:
                wins = [p for p in pnls if p > 0]
                print(f"    blocked-admit counter-factual: "
                      f"winners={len(wins)} losers={len(pnls)-len(wins)} "
                      f"mean={sum(pnls)/len(pnls):+.2f}%")
        if rule == "leverage_tier":
            for key in ("live_pnl_pct", "proposed_pnl_pct"):
                vals = [float(r[key]) for r in graded if key in r]
                if vals:
                    print(f"    {key:18} mean={sum(vals)/len(vals):+.2f}% "
                          f"sum={sum(vals):+.2f}%")
        if rule == "stop_tuning":
            green = sum(1 for r in graded
                        if r["outcome"] == "wider_cap_would_green")
            nogain = sum(1 for r in graded
                         if r["outcome"] == "wider_cap_no_gain")
            print(f"    wider cap would have turned GREEN: {green} ; "
                  f"still no gain: {nogain}")

    print("\n  per-record detail:")
    for r in sorted(records, key=lambda x: x.get("timestamp", "")):
        if r.get("outcome"):
            d = r.get("detail") or {}
            extra = ""
            if "pnl_pct" in r:
                extra = f"cf_pnl={r['pnl_pct']:+.2f}%"
            elif r["rule"] == "leverage_tier" and "live_pnl_pct" in r:
                extra = (f"live={r['live_pnl_pct']:+.1f}% "
                         f"prop={r['proposed_pnl_pct']:+.1f}%")
            elif r["rule"] == "stop_tuning":
                w = r.get("wider_cap_sim") or {}
                extra = f"wider_sim={w.get('pnl_pct')}%/{w.get('reason')}"
            print(f"    {r.get('timestamp')}  {r.get('rule'):22} "
                  f"{r.get('coin'):9} -> {r['outcome']:28} {extra}")


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
