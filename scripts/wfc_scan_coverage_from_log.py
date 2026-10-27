#!/usr/bin/env python3
"""WF-C 扫描覆盖：消费生产 coin_selection shadow 日志做实测覆盖分析（只读）。

生产 ``coin_selection.mode=shadow``（deploy/prod_override.json L89-98，top_k=55）
已在每个扫描周期落盘（perception.py scan_once）：

    ts / mode / eligible(分母) / bucket_k / score_k
    bucket_pick  = 实际拉 K 线的硬桶名单（42 vol+10 movers+8 sweep）
    score_pick   = 连续打分 top-55 反事实名单（不改实际扫描）
    selection_diff = 两名单对称差大小

本脚本把这份**已存在**的日志聚合成 WF-C 要的覆盖答案，避免在热路径新增重复
埋点：
  1. 单周期：eligible 分母、bucket_k、score_k、symdiff（top-55 与实际差多少）；
  2. 跨周期：bucket_pick 的去重并集覆盖了 eligible 宇宙的多少（实测，含 sweep
     旋转是否真的把长尾轮进来）、平均单周期覆盖率、每个币的扫描频率分位；
  3. top-55 反事实：它想扫但 bucket 长期漏掉的币（counterfactual 漏扫名单）。

纯只读、离线；找不到文件给空结论。部署并跑若干周期后再运行才有数据。

用法：
  python scripts/wfc_scan_coverage_from_log.py
  python scripts/wfc_scan_coverage_from_log.py --file /data/coin_selection.jsonl --days 14
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
from collections import Counter
from typing import Optional


def _default_path() -> str:
    for p in (
        os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"),
                     "coin_selection.jsonl"),
        os.path.expanduser("~/.hermes-trading/coin_selection.jsonl"),
    ):
        if os.path.exists(p):
            return p
    return os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"),
                        "coin_selection.jsonl")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=None)
    ap.add_argument("--days", type=float, default=None)
    args = ap.parse_args(argv)
    path = args.file or _default_path()

    if not os.path.exists(path):
        print(json.dumps({
            "file": path, "cycles": 0,
            "note": "coin_selection shadow log not present on this host; it is "
                    "written by the running container at $HERMES_DATA_DIR/"
                    "coin_selection.jsonl (mode=shadow, top_k=55). Re-run after "
                    "deploy once several scan cycles have elapsed.",
        }, ensure_ascii=False, indent=2))
        return 0

    cutoff = None
    if args.days:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=args.days)).timestamp() * 1000

    n = 0
    elig: list[int] = []
    bk: list[int] = []
    sk: list[int] = []
    diff: list[int] = []
    bucket_union: set[str] = set()
    score_union: set[str] = set()
    elig_union: set[str] = set()
    bucket_freq: Counter[str] = Counter()
    # 反事实想扫但 bucket 当周期漏掉
    score_only: Counter[str] = Counter()

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff is not None and float(r.get("ts") or 0) < cutoff:
                continue
            bp = r.get("bucket_pick") or []
            sp = r.get("score_pick") or []
            if not isinstance(bp, list):
                continue
            n += 1
            elig.append(int(r.get("eligible") or 0))
            bk.append(int(r.get("bucket_k") or len(bp)))
            sk.append(int(r.get("score_k") or len(sp)))
            diff.append(int(r.get("selection_diff") or 0))
            bset, sset = set(bp), set(sp)
            bucket_union |= bset
            score_union |= sset
            bucket_freq.update(bset)
            for c in sset - bset:
                score_only[c] += 1

    if not n:
        print(json.dumps({"file": path, "cycles": 0,
                          "note": "no usable shadow rows in window"}, indent=2))
        return 0

    # 用最大 eligible 分母近似窗口内宇宙规模（市场会上下线，取上界）。
    denom = max(max(elig), len(bucket_union))
    freqs = list(bucket_freq.values())
    out = {
        "file": path, "cycles": n,
        "window_days": args.days,
        "eligible_per_cycle": {"median": statistics.median(elig),
                               "min": min(elig), "max": max(elig)},
        "bucket_k_per_cycle": {"median": statistics.median(bk),
                               "min": min(bk), "max": max(bk)},
        "score_k_per_cycle": {"median": statistics.median(sk)},
        "selection_symdiff_per_cycle": {"median": statistics.median(diff),
                                        "p90": sorted(diff)[int(0.9 * (len(diff) - 1))]},
        "multi_cycle": {
            "distinct_coins_scanned": len(bucket_union),
            "universe_denom_max_eligible": denom,
            "union_coverage_pct": round(100.0 * len(bucket_union) / denom, 1)
            if denom else None,
            "avg_single_cycle_coverage_pct": round(
                100.0 * statistics.fmean(bk) / max(statistics.fmean(elig), 1), 1),
            "scan_freq": {
                "every_cycle": sum(1 for c in bucket_union
                                   if bucket_freq[c] >= 0.95 * n),
                "median_pct_of_cycles": round(
                    100.0 * statistics.median(freqs) / n, 1) if freqs else 0,
                "seen_once_pct_of_union": round(
                    100.0 * sum(1 for f in freqs if f == 1)
                    / max(len(bucket_union), 1), 1),
            },
        },
        "counterfactual_top55": {
            "distinct_score_picks": len(score_union),
            "score_only_coins_never_bucket_scanned": sorted(
                c for c in score_union - bucket_union)[:30],
            "top_score_only_by_cycles": score_only.most_common(15),
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
