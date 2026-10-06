#!/usr/bin/env python3
"""Outcome comparison for the strategy-paradigm optimisations (2026-10-04).

Joins the SHADOW-side signal logs to the realised outcomes recorded by the
shadow book, and prints the four comparisons the prioritisation matrix
promised. Read-only: it never writes config and never places orders.

Evidence sources (all timestamps are epoch MILLISECONDS):
  * result side  : /data/.shadow-book.json
                     accounts.taker.fills / accounts.maker_shadow.fills
                     (type=="close" rows carry realised pnl + reason)
  * signal side  : /data/signal_ranking.jsonl   (ranked candidates)
                   /data/conjunction_probe.jsonl (bullish_fired + AND-2/3)
  Both signal logs are stamped BEFORE research, so they carry no analysis_id;
  they are joined to a close fill on coin + a forward time window.
  The taker and maker_shadow close fills SHARE analysis_id, so the execution-
  cost comparison (P2) joins on that id directly.

Metrics per group: n, win rate, mean realised pnl (USD), expectancy per trade,
profit factor (gross win / gross loss), and the worst single loss. Small
samples are flagged rather than treated as proof.

Usage:
  python scripts/outcome_compare.py                 # human-readable report
  python scripts/outcome_compare.py --json out.json # machine-readable
  python scripts/outcome_compare.py --join-tol-s 300
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from typing import Any, Iterable

DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
BOOK_PATH = os.path.join(DATA_DIR, ".shadow-book.json")

# A signal is stamped a few seconds-to-minutes before the shadow position
# opens. Match a coin's signal to the nearest open within this forward window.
DEFAULT_JOIN_TOL_S = 300

# Below this many trades a group is reported as "thin" (not yet evidence).
MIN_SAMPLE = 20


# --------------------------------------------------------------------------- load

def _read_jsonl_with_rotations(base: str) -> list[dict[str, Any]]:
    """Read a jsonl plus its ``.1``..``.5`` rotated siblings (newest first)."""
    paths = [base] + sorted(glob.glob(base + ".[0-9]*"))
    rows: list[dict[str, Any]] = []
    for p in paths:
        try:
            with open(p) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def _close_fills(account: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in account.get("fills", []) if f.get("type") == "close"]


def load_data() -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                          list[dict[str, Any]], list[dict[str, Any]],
                          list[dict[str, Any]]]:
    with open(BOOK_PATH) as fh:
        book = json.load(fh)
    accounts = book.get("accounts", {})
    taker = _close_fills(accounts.get("taker", {}))
    maker = _close_fills(accounts.get("maker_shadow", {}))
    maker_cancels = [f for f in accounts.get("maker_shadow", {}).get("fills", [])
                     if f.get("type") == "cancel"]
    ranking = _read_jsonl_with_rotations(
        os.path.join(DATA_DIR, "signal_ranking.jsonl"))
    conj = _read_jsonl_with_rotations(
        os.path.join(DATA_DIR, "conjunction_probe.jsonl"))
    # Bundle the signal sources together for the join routine; they are
    # consumed separately by name below.
    return taker, maker, maker_cancels, ranking, conj


# --------------------------------------------------------------------------- stats

def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def group_stats(closes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate realised-pnl metrics over close fills."""
    rows = list(closes)
    n = len(rows)
    if n == 0:
        return {"n": 0}
    pnls = [_f(c.get("realized_pnl_usd")) for c in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    return {
        "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1),
        "mean_pnl_usd": round(sum(pnls) / n, 4),
        "expectancy_usd": round(sum(pnls) / n, 4),
        "gross_win_usd": round(gross_win, 4),
        "gross_loss_usd": round(gross_loss, 4),
        "profit_factor": (round(pf, 3) if pf != float("inf") else "inf"),
        "total_pnl_usd": round(sum(pnls), 4),
        "worst_loss_usd": round(min(pnls), 4),
        "thin": n < MIN_SAMPLE,
    }


def _group_by(rows: list[dict[str, Any]], key) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        k = key(r)
        out.setdefault(str(k), []).append(r)
    return out


# --------------------------------------------------------------------------- join

def _signal_to_close(signal_ts: int, coin: str,
                     closes: list[dict[str, Any]], tol_ms: int
                     ) -> dict[str, Any] | None:
    """Nearest close fill whose open is within [signal, signal+tol] on coin."""
    best = None
    best_dt = None
    for c in closes:
        if c.get("coin") != coin:
            continue
        opened = _f(c.get("opened_at"))
        dt = opened - signal_ts
        if 0 <= dt <= tol_ms and (best_dt is None or dt < best_dt):
            best, best_dt = c, dt
    return best


# --------------------------------------------------------------------------- analyses

def analyse_conjunction(conj: list[dict[str, Any]],
                        taker: list[dict[str, Any]], tol_ms: int
                        ) -> dict[str, Any]:
    """AND vs OR: group outcomes by #bullish triggers and AND gates."""
    by_fired: dict[str, list[dict]] = {}
    by_and2: dict[str, list[dict]] = {}
    for row in conj:
        coin = row.get("coin")
        close = _signal_to_close(int(_f(row.get("ts"))), coin, taker, tol_ms)
        if close is None:
            continue
        fired = int(_f(row.get("bullish_fired")))
        by_fired.setdefault(str(fired), []).append(close)
        passes2 = bool((row.get("passes_and") or {}).get("2"))
        by_and2.setdefault("AND>=2" if passes2 else "single (OR-only)",
                           []).append(close)
    return {
        "by_bullish_fired": {k: group_stats(v)
                              for k, v in sorted(by_fired.items())},
        "and2_vs_single": {k: group_stats(v)
                            for k, v in sorted(by_and2.items())},
    }


def analyse_ranking(ranking: list[dict[str, Any]],
                    taker: list[dict[str, Any]], tol_ms: int
                    ) -> dict[str, Any]:
    """Does a higher cross-signal rank score produce higher expectancy?"""
    buckets: dict[str, list[dict]] = {"score>=50": [], "30-50": [], "<30": []}
    for row in ranking:
        for item in row.get("ranking", []):
            close = _signal_to_close(
                int(_f(row.get("ts"))), item.get("coin"), taker, tol_ms)
            if close is None:
                continue
            s = _f(item.get("score"))
            key = ("score>=50" if s >= 50 else "30-50" if s >= 30 else "<30")
            buckets[key].append(close)
    return {k: group_stats(v) for k, v in buckets.items()}


def analyse_execution(taker: list[dict[str, Any]],
                      maker: list[dict[str, Any]],
                      cancels: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """P2: paired taker vs maker_shadow outcomes on shared analysis_id.

    除"双方都成交"的配对外，还纳入 maker 的踏空口径（M-5 否决切换的关键项）：
    TTL 内未触及被撤销的单 = maker 踏空；miss_rate=踏空/(成交+踏空)；
    机会成本=这些踏空单按 taker 同 analysis_id 的已实现 PnL 之和（maker 错过的
    部分）。只看成交配对会系统性高估 maker，故此处必须同时报告。
    """
    cancels = cancels or []
    maker_by_id = {m.get("analysis_id"): m for m in maker
                   if m.get("analysis_id")}
    taker_by_id = {t.get("analysis_id"): t for t in taker
                   if t.get("analysis_id")}
    paired_t, paired_m = [], []
    for t in taker:
        m = maker_by_id.get(t.get("analysis_id"))
        if m is not None:
            paired_t.append(t)
            paired_m.append(m)

    # 踏空：cancel 单（同一信号 taker 成交了，maker 没成交）
    missed_ids = {c.get("analysis_id") for c in cancels if c.get("analysis_id")}
    missed_taker = [taker_by_id[i] for i in missed_ids if i in taker_by_id]
    n_filled = len(paired_m)
    n_miss = len(missed_taker)
    n_attempt = n_filled + n_miss
    opp_cost = sum(_f(t.get("realized_pnl_usd")) for t in missed_taker)

    return {
        "taker_all": group_stats(taker),
        "taker_paired": group_stats(paired_t),
        "maker_paired": group_stats(paired_m),
        "paired_count": len(paired_t),
        "mean_pnl_diff_maker_minus_taker_usd": (
            round((sum(_f(m.get("realized_pnl_usd")) for m in paired_m)
                   - sum(_f(t.get("realized_pnl_usd")) for t in paired_t))
                  / max(1, len(paired_t)), 4)),
        "maker_miss_count": n_miss,
        "maker_miss_rate_pct": round(100.0 * n_miss / n_attempt, 1)
                               if n_attempt else 0.0,
        "maker_missed_opportunity_cost_usd": round(opp_cost, 4),
        "maker_net_edge_vs_taker_usd": round(
            # 成交配对上的累计改善 − 踏空错过的 PnL
            (sum(_f(m.get("realized_pnl_usd")) for m in paired_m)
             - sum(_f(t.get("realized_pnl_usd")) for t in paired_t))
            - opp_cost, 4),
    }


def analyse_risk(taker: list[dict[str, Any]]) -> dict[str, Any]:
    """Risk-side picture relevant to active unstucking."""
    if not taker:
        return {}
    pnls = [_f(c.get("realized_pnl_usd")) for c in taker]
    # worst consecutive realised drawdown (closed-trade basis)
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    worst_reason = min(taker, key=lambda c: _f(c.get("realized_pnl_usd")))
    return {
        "closed_trades": len(taker),
        "worst_single_loss_usd": round(min(pnls), 4),
        "max_closed_drawdown_usd": round(max_dd, 4),
        "loss_exits": sum(1 for p in pnls if p < 0),
        "worst_exit_reason": worst_reason.get("reason"),
    }


# --------------------------------------------------------------------------- report

def _fmt(stats: dict[str, Any]) -> str:
    if not stats or stats.get("n", 0) == 0:
        return "n=0"
    tag = " [thin]" if stats.get("thin") else ""
    return (f"n={stats['n']:<3} win={stats['win_rate']:>5}%  "
            f"expect={stats['expectancy_usd']:>8}  PF={stats['profit_factor']:<6}"
            f" worst={stats['worst_loss_usd']:>8}{tag}")


def print_coverage(conj, ranking, taker, tol_s) -> None:
    """Report how much the recent signal logs overlap outcome-producing opens."""
    tol_ms = tol_s * 1000
    if conj:
        c0 = min(int(_f(r.get("ts"))) for r in conj)
        c1 = max(int(_f(r.get("ts"))) for r in conj)
    else:
        c0 = c1 = 0
    if taker:
        o0 = min(int(_f(c.get("opened_at"))) for c in taker)
        o1 = max(int(_f(c.get("opened_at"))) for c in taker)
    else:
        o0 = o1 = 0
    matched = sum(
        1 for r in conj
        if _signal_to_close(int(_f(r.get("ts"))), r.get("coin"), taker, tol_ms)
        is not None)
    print(f"conjunction signal window : {c0} .. {c1} (n={len(conj)})")
    print(f"shadow open window        : {o0} .. {o1} (closes n={len(taker)})")
    print(f"matched within {tol_s}s      : {matched} of {len(conj)} signals")
    print("If matched << n, the signal probe and outcome-bearing trades do not "
          "yet overlap; keep shadow running before drawing AND/OR conclusions.")


def print_report(result: dict[str, Any]) -> None:
    print("=" * 78)
    print("Hermes strategy-paradigm outcome comparison (SHADOW book, read-only)")
    print("=" * 78)

    print("\n[P0] OR -> conjunction (does requiring >=2 triggers help?)")
    conj = result["conjunction"]
    print("  by #bullish triggers:")
    for k, v in conj["by_bullish_fired"].items():
        print(f"    fired={k:<3} {_fmt(v)}")
    print("  AND gate vs single:")
    for k, v in conj["and2_vs_single"].items():
        print(f"    {k:<18} {_fmt(v)}")

    print("\n[P0] cross-signal rank score vs realised outcome:")
    for k, v in result["ranking"].items():
        print(f"    {k:<10} {_fmt(v)}")

    print("\n[P2] execution: taker vs maker_shadow (paired on analysis_id):")
    ex = result["execution"]
    print(f"    taker all   {_fmt(ex['taker_all'])}")
    print(f"    taker paired {_fmt(ex['taker_paired'])}")
    print(f"    maker paired {_fmt(ex['maker_paired'])}")
    print(f"    paired trades        : {ex['paired_count']}")
    print(f"    mean maker - taker   : {ex['mean_pnl_diff_maker_minus_taker_usd']} USD")

    print("\n[P1] risk picture (for active unstucking):")
    for k, v in result["risk"].items():
        print(f"    {k:<28} {v}")

    print("\nNote: groups marked [thin] have n < %d and are NOT yet evidence."
          % MIN_SAMPLE)
    print("This report describes SHADOW outcomes only; it changes no config.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write machine-readable JSON to this path")
    ap.add_argument("--join-tol-s", type=int, default=DEFAULT_JOIN_TOL_S,
                    help="signal->open forward match window in seconds")
    ap.add_argument("--coverage", action="store_true",
                    help="print signal/outcome overlap coverage and exit")
    args = ap.parse_args()
    tol_ms = args.join_tol_s * 1000

    taker, maker, maker_cancels, ranking, conj = load_data()

    if args.coverage:
        print_coverage(conj, ranking, taker, args.join_tol_s)
        return

    result = {
        "data_dir": DATA_DIR,
        "join_tol_s": args.join_tol_s,
        "conjunction": analyse_conjunction(conj, taker, tol_ms),
        "ranking": analyse_ranking(ranking, taker, tol_ms),
        "execution": analyse_execution(taker, maker, maker_cancels),
        "risk": analyse_risk(taker),
    }

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        print(f"wrote {args.json}")
    else:
        print_report(result)


if __name__ == "__main__":
    main()
