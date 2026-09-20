#!/usr/bin/env python3
"""C-7 留一月份法（leave-one-month-out）按天块自助。

对 1h（或任意）回放产物，按 ADR-0002 预注册方案剔除指定自然月后重算按天块自助
95% CI，检验 edge 是否由单月驱动（P2：剔 6 月后 4h edge 崩到 +4.59/t0.50，
9 月显著为负 -60.88）。月份按 entry_t 的 UTC 年-月剔除。

用法：
  python scripts/leave_one_month_bootstrap.py --trades logs/c7_81coin_exit_1h.jsonl \
      --arms filt,filt_exch --drop 2026-06 --drop 2026-09
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import random
import statistics


def month_of(ms: int) -> str:
    return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m")


def ci_for(byday: dict[int, list[float]], boot: int, rng: random.Random):
    days = [statistics.mean(v) for v in byday.values()]
    D = len(days)
    if D < 2:
        return None
    vals = [statistics.mean([days[rng.randrange(D)] for _ in range(D)])
            for _ in range(boot)]
    vals.sort()
    return statistics.mean([x for v in byday.values() for x in v]), D, \
        vals[int(boot * .025)], vals[int(boot * .975)]


def run(path: str, arms: tuple[str, ...], drops: tuple[str, ...],
        boot: int, seed: int) -> None:
    kept = {a: collections.defaultdict(list) for a in arms}
    dropped_n = collections.Counter()
    with open(path) as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("type") != "trade" or d.get("arm") not in arms or not d.get("notional"):
                continue
            m = month_of(d["entry_t"])
            if m in drops:
                dropped_n[m] += 1
                continue
            kept[d["arm"]][d["entry_t"] // 86_400_000].append(
                d["pnl_net"] / d["notional"] * 1e4)

    drop_label = ",".join(drops) if drops else "(none)"
    print(f"剔除月份 {drop_label}；剔除数 {dict(dropped_n)}；块自助 {boot} 次 seed={seed}")
    print(f"{'arm':11s} {'笔':>6s} {'天':>4s} {'per-trade bps':>14s} {'95%CI':>24s}  含0?")
    for arm in arms:
        rng = random.Random(seed)
        res = ci_for(kept[arm], boot, rng)
        if res is None:
            print(f"{arm:11s}  (样本不足)")
            continue
        mean, D, lo, hi = res
        verdict = ("含0(不显著)" if lo <= 0 <= hi
                   else "显著为负" if hi < 0 else "显著为正")
        n = sum(len(v) for v in kept[arm].values())
        print(f"{arm:11s} {n:6d} {D:4d} {mean:14.2f} [{lo:10.2f},{hi:10.2f}]  {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="logs/c7_81coin_exit_1h.jsonl")
    ap.add_argument("--arms", default="filt,filt_exch,filt_ra,baseline")
    ap.add_argument("--drop", action="append", default=[],
                    help="要剔除的 UTC 年-月，可重复，如 --drop 2026-06")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260920)
    a = ap.parse_args()
    run(a.trades, tuple(a.arms.split(",")), tuple(a.drop), a.boot, a.seed)
