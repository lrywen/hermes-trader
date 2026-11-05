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
from datetime import datetime, timezone
from typing import Optional

from hermes_trader.validation import (
    block_bootstrap_ci,
    cpcv_paths,
    day_bps_series,
    negative_oos_fold_ratio,
    sharpe,
)
from hermes_trader.validation.trial_ledger import (
    TrialLedger,
    TrialLedgerError,
    deflated_sharpe_from_ledger,
)

DSR_ACCEPT_P = 0.95


def _input_as_of(path: str, arms: tuple[str, ...]) -> Optional[str]:
    """Latest source-trade timestamp, used as one auditable as-of for all arms."""
    latest = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("type") != "trade" or rec.get("arm") not in arms:
                continue
            try:
                ts = int(rec["entry_t"])
            except (KeyError, TypeError, ValueError):
                continue
            latest = ts if latest is None else max(latest, ts)
    if latest is None:
        return None
    return datetime.fromtimestamp(latest / 1000, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def evaluate(
        path: str,
        arms: tuple[str, ...],
        boot: int,
        seed: int,
        n_trials: Optional[int] = None,
        *,
        as_of=None,
        ledger_path=None,
        ledger: Optional[TrialLedger] = None,
        output_path: Optional[str] = "/tmp/validate_outcome.json") -> dict:
    """Validate every arm against its own ``arm×daily_outcome`` ledger family.

    ``n_trials`` is accepted only for positional compatibility and is ignored.
    Any family/as-of/ledger failure is returned as machine-readable evidence and
    makes the overall verdict fail closed; no implicit multiplicity is used.
    """
    as_of = as_of or _input_as_of(path, arms)
    rows: list[dict] = []
    dsr_errors: list[dict] = []

    def attach_dsr(row: dict, arm: str, sr: float) -> None:
        try:
            evidence = deflated_sharpe_from_ledger(
                observed_sharpe=sr,
                n_obs=row["days"],
                arm=arm,
                paradigm="daily_outcome",
                as_of=as_of,
                ledger_path=ledger_path,
                ledger=ledger,
            )
        except TrialLedgerError as exc:
            error = exc.as_dict()
            error["arm"] = arm
            row.update({
                "dsr_p": None,
                "n_trials": None,
                "trial_evidence": None,
                "dsr_error": error,
                "dsr_statistical_passes": False,
                "dsr_promotion_eligible": False,
            })
            dsr_errors.append(error)
            return
        row.update({
            "dsr_p": round(evidence["dsr_p"], 4),
            "n_trials": evidence["n_trials"],
            "trial_evidence": evidence,
            "dsr_error": None,
            "dsr_statistical_passes": evidence["dsr_p"] >= DSR_ACCEPT_P,
            "dsr_promotion_eligible": evidence["promotion_eligible"],
        })

    for arm in arms:
        s = day_bps_series(path, arm)
        if len(s) < 12:
            rows.append({"arm": arm, "skip": "样本不足(<12 天)"})
            continue
        lo, hi = block_bootstrap_ci(s, boot, seed)
        cpcv = cpcv_paths(s, n_groups=6, n_test_groups=2, embargo=1)
        sr = sharpe(s)
        # H3: this is the OOS-negative fold ratio, NOT CSCV overfitting prob.
        oos_neg = negative_oos_fold_ratio(list(cpcv.oos_sharpe))
        row = {
            "arm": arm,
            "days": len(s),
            "bb_ci": [round(lo, 2), round(hi, 2)],
            "bb_positive": lo > 0,
            "cpcv_oos_pos_frac": round(cpcv.oos_win_frac, 3),
            "sharpe": round(sr, 3),
            "oos_negative_fold_ratio": round(oos_neg, 3),
        }
        attach_dsr(row, arm, sr)
        rows.append(row)

    any_pos = any(r.get("bb_positive") and r.get("dsr_statistical_passes")
                  for r in rows if "skip" not in r)
    statistical_verdict = (
        "POSITIVE_EDGE_POSSIBLE" if any_pos else "NEGATIVE_EXPECTANCY")
    promotion_eligible = not dsr_errors and any(
        r.get("bb_positive") and r.get("dsr_statistical_passes")
        and r.get("dsr_promotion_eligible")
        for r in rows if "skip" not in r)
    result = {
        "verdict": "fail_closed" if dsr_errors else statistical_verdict,
        "statistical_verdict": statistical_verdict,
        "as_of": as_of,
        "arms": rows,
        "dsr_errors": dsr_errors,
        "promotion_eligible": promotion_eligible,
        "promotion_block": (
            "dsr_fail_closed" if dsr_errors else
            None if promotion_eligible else "legacy_import_only"),
    }
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="logs/b3_81coin_filt_ra.jsonl")
    ap.add_argument("--arms", default="filt,filt_exch,filt_ra,baseline")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260920)
    ap.add_argument("--as-of", default=None,
                    help="DSR 账本截止时刻；默认取输入交易的最新 entry_t")
    ap.add_argument("--trial-ledger", default=None,
                    help="DSR 试验账本 JSONL（默认读包内账本或环境变量）")
    a = ap.parse_args()
    r = evaluate(a.input, tuple(a.arms.split(",")), a.boot, a.seed,
                 as_of=a.as_of, ledger_path=a.trial_ledger)

    print(f"verdict = {r['verdict']}")
    print(f"{'arm':11s} {'天':>4s} {'block-bootstrap 95%CI':>26s} "
          f"{'CPCV OOS正占比':>14s} {'SR':>7s} {'DSR P':>7s} {'OOS负折':>7s}")
    for x in r["arms"]:
        if "skip" in x:
            print(f"{x['arm']:11s}   {x['skip']}")
        else:
            lo, hi = x["bb_ci"]
            dsr_text = (f"{x['dsr_p']:7.4f}" if x.get("dsr_p") is not None
                        else "  closed")
            print(f"{x['arm']:11s} {x['days']:4d} [{lo:9.2f},{hi:9.2f}] "
                  f"{x['cpcv_oos_pos_frac']:14.2f} {x['sharpe']:7.3f} "
                  f"{dsr_text} {x['oos_negative_fold_ratio']:7.3f}")
    print("\n明细 → /tmp/validate_outcome.json")


if __name__ == "__main__":
    main()