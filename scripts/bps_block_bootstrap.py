#!/usr/bin/env python3
"""按天块自助（bps/笔口径）——规避等权 notional 假设的显著性标准件。

标准 ``backtest_block_bootstrap.py`` 用单一 ``notional_bt`` 把每笔 pnl 换算 bps，
再乘目标 notional 得美元；当输入 JSONL 里**混合了不同 notional 的臂**（B-3 的
filt/filt_ra 等权 10000，而 filt_exch 是 live-sizing 2.08~30 逐笔变）时，对
live-sizing 臂会二次缩放，美元 CI 失真。本脚本每笔用【自身 notional】算净 bps，
按天取均值再有放回抽天，得到各臂在「每笔等权 bps」口径下可横比的 95% CI。

为什么抽天不抽笔：同一天多笔共享同一段行情，有效独立样本是天数而非笔数
（单笔 t 检验系统性高估显著性）。maxc 组合容量结论不在本件，见 G2-L5 / P3-4。
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics

from hermes_trader.validation import block_bootstrap_ci


def run(path: str, arms: tuple[str, ...], boot: int, seed: int) -> None:
    byday: dict[str, dict[int, list[float]]] = {a: collections.defaultdict(list) for a in arms}
    with open(path) as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("type") != "trade" or d.get("arm") not in arms or not d.get("notional"):
                continue
            byday[d["arm"]][d["entry_t"] // 86_400_000].append(d["pnl_net"] / d["notional"] * 1e4)

    print(f"{'arm':11s} {'笔':>6s} {'天':>4s} {'per-trade bps':>14s} "
          f"{'按天块自助95%CI':>24s}  含0?")
    for arm in arms:
        days = byday[arm]
        if not days:
            print(f"{arm:11s}  (无样本)")
            continue
        day_means = [statistics.mean(v) for v in days.values()]
        allv = [x for v in days.values() for x in v]
        lo, hi = block_bootstrap_ci(day_means, boot, seed)
        verdict = ("含0(不显著)" if lo <= 0 <= hi
                   else "显著为负" if hi < 0 else "显著为正")
        print(f"{arm:11s} {len(allv):6d} {len(day_means):4d} {statistics.mean(allv):14.2f} "
              f"[{lo:10.2f},{hi:10.2f}]  {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="logs/b3_81coin_filt_ra.jsonl")
    ap.add_argument("--arms", default="filt,filt_exch,filt_ra,baseline")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260920)
    a = ap.parse_args()
    run(a.input, tuple(a.arms.split(",")), a.boot, a.seed)
