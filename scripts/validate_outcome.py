#!/usr/bin/env python3
"""统一策略结局验证入口：一次跑全三种独立显著性方法，输出二值判定。

把原先分散在两处的检验串联为一个入口，消除"每次判断都要手动调多个脚本"的
出错面，同时不替换任何现有实现（block-bootstrap 脚本仍在、validation 包仍在）。

三方法（共享同一按天 bps 数据契约）：
  1. block-bootstrap —— 按天块自助 95% CI（与 scripts/bps_block_bootstrap.py 同口径）；
  2. CPCV —— 组合 purged 交叉验证，多路样本外夏普；
  3. DSR / oos_negative_fold_ratio —— 多重检验校正后"真实夏普为正"的概率 /
     样本外负收益折叠占比。

H3 (2026-11-04): 第三个量原字段名 `pbo` 是误称 — probability_of_backtest_overfitting
在此数据契约下实际返回的是「WFA/CPCV 样本外夏普为负的折叠比例」，不是 Bailey/López
de Prado CSCV 定义的过拟合概率（真 CSCV 需要一个配置矩阵，单策略无该矩阵，不构造
假数）。字段已改名为 oos_negative_fold_ratio；所有"过拟合概率"措辞撤回。真 CSCV
单独立项。

最终 "verdict" 为二值：
  NEGATIVE_EXPECTANCY（三方法一致为负，等同 OUTCOME_B）
  | POSITIVE_EDGE_POSSIBLE（CI 下界 >0 且 DSR P>阈值，需人工复核）

用法：
  .venv/bin/python scripts/validate_outcome.py --input logs/b3_81coin_filt_ra.jsonl \
      --arms filt,filt_exch,filt_ra,baseline
"""
from __future__ import annotations

import argparse
import json

from hermes_trader.validation import (
    block_bootstrap_ci,
    cpcv_paths,
    day_bps_series,
    deflated_sharpe_prob,
    probability_of_backtest_overfitting,
    sharpe,
)

DSR_ACCEPT_P = 0.95


def evaluate(path: str, arms: tuple[str, ...], boot: int, seed: int,
             n_trials: int) -> dict:
    rows: list[dict] = []
    for arm in arms:
        s = day_bps_series(path, arm)
        if len(s) < 12:
            rows.append({"arm": arm, "skip": "样本不足(<12 天)"})
            continue
        lo, hi = block_bootstrap_ci(s, boot, seed)
        cpcv = cpcv_paths(s, n_groups=6, n_test_groups=2, embargo=1)
        sr = sharpe(s)
        dsr = deflated_sharpe_prob(sr, n_trials=n_trials, n_obs=len(s))
        # H3: this is the OOS-negative fold ratio, NOT CSCV overfitting prob.
        oos_neg = probability_of_backtest_overfitting([sr] * cpcv.n_paths,
                                                      list(cpcv.oos_sharpe))
        rows.append({
            "arm": arm,
            "days": len(s),
            "bb_ci": [round(lo, 2), round(hi, 2)],
            "bb_positive": lo > 0,
            "cpcv_oos_pos_frac": round(cpcv.oos_win_frac, 3),
            "sharpe": round(sr, 3),
            "dsr_p": round(dsr, 4),
            "oos_negative_fold_ratio": round(oos_neg, 3),
        })

    # 二值判定：存在任一臂 bb_positive 且 dsr_p 通过，才可能为 edge
    any_pos = any(r.get("bb_positive") and r.get("dsr_p", 0) >= DSR_ACCEPT_P
                  for r in rows if "skip" not in r)
    verdict = "POSITIVE_EDGE_POSSIBLE" if any_pos else "NEGATIVE_EXPECTANCY"
    result = {"verdict": verdict, "arms": rows}
    json.dump(result, open("/tmp/validate_outcome.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="logs/b3_81coin_filt_ra.jsonl")
    ap.add_argument("--arms", default="filt,filt_exch,filt_ra,baseline")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260920)
    ap.add_argument("--n-trials", type=int, default=16,
                    help="DSR 多重试验次数口径（臂数×已试范式/周期）")
    a = ap.parse_args()
    r = evaluate(a.input, tuple(a.arms.split(",")), a.boot, a.seed, a.n_trials)

    print(f"verdict = {r['verdict']}")
    print(f"{'arm':11s} {'天':>4s} {'block-bootstrap 95%CI':>26s} "
          f"{'CPCV OOS正占比':>14s} {'SR':>7s} {'DSR P':>7s} {'OOS负折':>7s}")
    for x in r["arms"]:
        if "skip" in x:
            print(f"{x['arm']:11s}   {x['skip']}")
        else:
            lo, hi = x["bb_ci"]
            print(f"{x['arm']:11s} {x['days']:4d} [{lo:9.2f},{hi:9.2f}] "
                  f"{x['cpcv_oos_pos_frac']:14.2f} {x['sharpe']:7.3f} "
                  f"{x['dsr_p']:7.4f} {x['oos_negative_fold_ratio']:7.3f}")
    print("\n明细 → /tmp/validate_outcome.json")


if __name__ == "__main__":
    main()