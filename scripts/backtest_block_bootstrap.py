#!/usr/bin/env python3
"""backtest_block_bootstrap.py — 把「按天块自助」固化为回测后处理标准件。

为什么必须用块自助（而不是单笔 t 检验）
────────────────────────────────────────────────────────────────────────────
单笔 t 检验假设样本独立。但交易在时间上高度聚集：同一天多笔共享同一段行情，
同一个月多笔共享同一个 regime。**有效独立样本 = 天数，不是笔数。**

实测（本项目，filt 臂 h240）：
    单笔 t 检验      t = 2.15   CI 不含 0   -> "显著"
    按天块自助 95%CI [−8.16, +47.13]       -> 跨 0，不显著
    CI 宽 1.5–1.9 倍。

本项目 394–501 笔交易实际只分布在 137 天里。用笔数算自由度会系统性高估显著性。

算法
────
有放回地抽【天】（不是抽笔），保留天内相关性；重复 B 次，得到统计量分布。
索引必须用 len(boot) 而非预设长度（天内笔数不等长）。

用法
────
  python scripts/backtest_block_bootstrap.py --input /tmp/btc_B_majors.jsonl \\
      --arms filt,baseline --boot 2000

  # 叠加实盘并发约束（max_concurrent）与真实资金口径
  python scripts/backtest_block_bootstrap.py --input /tmp/btc_B_majors.jsonl \\
      --arms filt --maxc 2 --equity 30.58 --notional 30

  # 剔除指定月份（留一期法）
  python scripts/backtest_block_bootstrap.py --input ... --drop-months 2026-06,2026-08
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 复用内核 guard 的实盘并发硬上限（B-2），避免后处理再放宽口径。
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from hermes_trader.backtest import guard as kguard


def _tstat(v: List[float]) -> float:
    if len(v) < 2:
        return 0.0
    sd = statistics.stdev(v)
    return statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0


def _load(path: str, arms: Optional[set], notional_bt: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "trade":
                continue
            if arms and r.get("arm") not in arms:
                continue
            d = dt.datetime.utcfromtimestamp(r["entry_t"] / 1000)
            r["_day"] = d.strftime("%Y-%m-%d")
            r["_month"] = d.strftime("%Y-%m")
            r["_bps"] = r["pnl_net"] / notional_bt * 1e4
            rows.append(r)
    rows.sort(key=lambda r: r["entry_t"])
    return rows


def _apply_maxc(rows: List[Dict[str, Any]], maxc: int) -> List[Dict[str, Any]]:
    """实盘并发上限：按 entry_t 顺序执行，槽位满则丢弃该信号。

    B-2：上限经内核 guard 校验，> 实盘峰值并发（2）硬拒绝——本工具不得再构造
    比生产更宽松的并发口径（P3-4：filt 无约束 +13.86% → maxc=2 −5.20%）。
    """
    kguard.assert_max_concurrent_allowed(maxc)
    open_exits: List[int] = []
    keep: List[Dict[str, Any]] = []
    for r in rows:
        open_exits = [e for e in open_exits if e > r["entry_t"]]
        if len(open_exits) >= maxc:
            continue
        keep.append(r)
        open_exits.append(r["exit_t"])
    return keep


def _block_bootstrap_usd(rows: List[Dict[str, Any]], notional: float,
                         boot: int, seed: int) -> tuple:
    """有放回抽【天】，返回 (lo, hi, D, mean)。"""
    rng = random.Random(seed)
    byday: Dict[str, float] = collections.defaultdict(float)
    for r in rows:
        byday[r["_day"]] += notional * r["pnl_net"] / 10000.0
    days = list(byday)
    D = len(days)
    if D < 2:
        return float("nan"), float("nan"), D, float("nan")
    out: List[float] = []
    for _ in range(boot):
        s = 0.0
        for _ in range(D):
            s += byday[days[rng.randrange(D)]]
        out.append(s)
    out.sort()
    k = len(out)
    return out[int(k * .025)], out[int(k * .975)], D, statistics.mean(out)


def _perf_usd(rows: List[Dict[str, Any]], notional: float, equity: float) -> Dict[str, float]:
    byday: Dict[str, float] = collections.defaultdict(float)
    for r in rows:
        byday[r["_day"]] += notional * r["pnl_net"] / 10000.0
    days = sorted(byday)
    eq = equity
    peak = equity
    mdd = 0.0
    seq: List[float] = []
    for d in days:
        eq += byday[d]
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        seq.append(byday[d] / equity)
    return {
        "usd": sum(byday.values()),
        "pct": sum(byday.values()) / equity * 100,
        "mdd": mdd * 100,
        "sharpe": (statistics.mean(seq) / statistics.stdev(seq) * math.sqrt(365)
                   if len(seq) > 1 and statistics.stdev(seq) else 0.0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="回测输出的 JSONL（--write 产物）")
    ap.add_argument("--arms", default="", help="逗号分隔臂名；空=全部")
    ap.add_argument("--boot", type=int, default=2000, help="自助次数（默认 2000）")
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--notional-bt", type=float, default=10000.0,
                    help="回测落库时的每笔名义（用于还原 bps；默认 10000）")
    ap.add_argument("--notional", type=float, default=0.0,
                    help="实盘每笔名义（美元）。给定则按实盘美元口径输出")
    ap.add_argument("--equity", type=float, default=0.0, help="实盘权益（美元）")
    ap.add_argument("--maxc", type=int, default=kguard.MAX_CONCURRENT_POSITIONS,
                    help="并发上限（B-2 默认实盘硬上限 2；显式给 1 更保守；>2 经 "
                         "内核 guard 硬拒绝）")
    ap.add_argument("--drop-months", default="", help="逗号分隔 YYYY-MM，留一期法")
    args = ap.parse_args()

    arms = {a.strip() for a in args.arms.split(",") if a.strip()} or None
    rows = _load(args.input, arms, args.notional_bt)
    if not rows:
        print("no trades loaded", file=sys.stderr)
        sys.exit(1)

    drop_m = {m.strip() for m in args.drop_months.split(",") if m.strip()}
    if drop_m:
        rows = [r for r in rows if r["_month"] not in drop_m]

    groups: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        groups[r["arm"]].append(r)

    print(f"input: {args.input}   boot: {args.boot}   seed: {args.seed}"
          + (f"   drop-months: {sorted(drop_m)}" if drop_m else "")
          + (f"   maxc: {args.maxc}" if args.maxc else ""))

    if args.notional and args.equity:
        print(f"\n{'臂':<14}{'信号N':>7}{'执行N':>7}{'天':>5}{'美元':>9}{'占权益%':>10}"
              f"{'回撤%':>9}{'夏普':>8}{'块自助95%CI(美元)':>26}")
        for arm in sorted(groups):
            g = groups[arm]
            sub = _apply_maxc(g, args.maxc)
            if not sub:
                continue
            lo, hi, D, _ = _block_bootstrap_usd(sub, args.notional, args.boot, args.seed)
            p = _perf_usd(sub, args.notional, args.equity)
            print(f"{arm:<14}{len(g):>7}{len(sub):>7}{D:>5}{p['usd']:>+9.2f}{p['pct']:>+10.2f}"
                  f"{p['mdd']:>9.2f}{p['sharpe']:>8.2f}{f'[{lo:+.2f}, {hi:+.2f}]':>26}"
                  f"{'  ✓' if lo > 0 else '  ✗'}")
    else:
        print(f"\n{'臂':<14}{'N':>7}{'天':>5}{'逐笔bps':>10}{'t(单笔)':>10}"
              f"{'块自助95%CI(bps)':>26}{'宽比':>7}")
        for arm in sorted(groups):
            g = groups[arm]
            sub = _apply_maxc(g, args.maxc)
            if len(sub) < 5:
                continue
            bps = [r["_bps"] for r in sub]
            t1 = _tstat(bps)
            rng = random.Random(args.seed)
            byday: Dict[str, List[float]] = collections.defaultdict(list)
            for r in sub:
                byday[r["_day"]].append(r["_bps"])
            days = list(byday)
            D = len(days)
            boot: List[float] = []
            for _ in range(args.boot):
                pool: List[float] = []
                for _ in range(D):
                    pool.extend(byday[days[rng.randrange(D)]])
                if pool:
                    boot.append(statistics.mean(pool))
            boot.sort()
            k = len(boot)
            lo, hi = boot[int(k * .025)], boot[int(k * .975)]
            se1 = statistics.stdev(bps) / math.sqrt(len(bps))
            w1 = 2 * 1.96 * se1
            wb = hi - lo
            print(f"{arm:<14}{len(sub):>7}{D:>5}{statistics.mean(bps):>+10.2f}{t1:>10.2f}"
                  f"{f'[{lo:+.2f}, {hi:+.2f}]':>26}{wb/w1 if w1 else float('nan'):>7.2f}"
                  f"{'  ✓' if lo > 0 else '  ✗'}")
        print("\n  注：'宽比' = 块自助 CI 宽 / 单笔 t 的 95% CI 宽。>1 说明单笔 t 低估了不确定性。")


if __name__ == "__main__":
    main()
