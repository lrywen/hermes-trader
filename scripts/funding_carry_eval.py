#!/usr/bin/env python3
"""Funding/premium 极端拥挤后的 delta-neutral carry 检验。

假设
----
M-2~M-10、V-01 已证明币级"方向预测"剥 beta 后 edge 归零。本检验转向**不依赖
方向预测**的结构性收益：当 funding 与 premium 同时极端（单边杠杆极度拥挤），
建立 delta-neutral 反向仓（正 funding -> 空 perp / 多现货；负 funding 反向），
持有固定窗口后平仓，收益来自两处均值回复：

1. **票息 coupon**：持有期实际结算的 funding（用"进场后真实 funding"，不假设
   维持高位）。
2. **basis**：进场 -> 出场的 premium 变化（正 funding 时空 perp，premium 收窄
   有利；负 funding 反之）。

成本：两腿（perp + 现货）开+平的 taker 费。HL base tier：perp 4.5bp/侧、
现货 7bp/侧 -> 2×(4.5+7) ≈ 23bp；保守用 ``--cost-bp`` 默认 32bp 预留滑点。

数据：``${HERMES_DATA_DIR}/funding/{coin}.jsonl.gz``（collect_funding.py 产物，
字段 {coin,fundingRate,premium,time}）。信号阈值/窗口在全样本直接扫描（本脚本为
可行性探索；进入交易前需另做样本外/forward）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
from glob import glob
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


def load_panel(funding_root: Path):
    panel = {}
    for f in sorted(glob(str(funding_root / "*.jsonl.gz"))):
        coin = Path(f).name.split(".")[0]
        rows = [json.loads(x) for x in gzip.open(f, "rt") if x.strip()]
        rows.sort(key=lambda r: r["time"])
        panel[coin] = rows
    return panel


def evaluate(panel, *, th, hold_h, cost_bp):
    coupons, bases, totals = [], [], []
    bycoin: dict[str, list[float]] = {}
    n_entries = 0
    for coin, rows in panel.items():
        ra = [float(r["fundingRate"]) for r in rows]
        pa = [float(r["premium"]) for r in rows]
        for i in range(len(rows)):
            f0 = ra[i]
            if abs(f0) < th or i + hold_h >= len(rows):
                continue
            n_entries += 1
            cum = sum(ra[i + 1:i + hold_h + 1])
            coupon = (cum if f0 > 0 else -cum) * 1e4 - cost_bp
            db = (pa[i] - pa[i + hold_h]) if f0 > 0 \
                else (pa[i + hold_h] - pa[i])
            basis = db * 1e4
            net = coupon + basis
            coupons.append(coupon)
            bases.append(basis)
            totals.append(net)
            bycoin.setdefault(coin, []).append(net)
    loo = []
    for coin in bycoin:
        rest = [z for c, xs in bycoin.items() if c != coin for z in xs]
        if rest:
            loo.append((coin, statistics.mean(rest)))
    return {
        "n_entries": n_entries,
        "coupon": _desc(coupons),
        "basis": _desc(bases),
        "total": _desc(totals),
        "n_coins": len(bycoin),
        "loo_min": (min(loo, key=lambda t: t[1]) if loo else (None, float("nan"))),
        "loo_max": (max(loo, key=lambda t: t[1]) if loo else (None, float("nan"))),
        "loo_pos": sum(1 for _, m in loo if m > 0),
        "loo_n": len(loo),
    }


def _desc(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return {"n": n, "mean": statistics.mean(s),
            "median": statistics.median(s),
            "p10": s[n // 10], "p90": s[n * 9 // 10],
            "win": sum(1 for z in s if z > 0) / n}


def _print_desc(tag, d):
    if not d:
        print(f"{tag}: (empty)")
        return
    print(f"{tag:<24} n={d['n']:>5} mean {d['mean']:7.1f} "
          f"median {d['median']:7.1f} p10 {d['p10']:7.1f} "
          f"p90 {d['p90']:7.1f} win {d['win']:.3f}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--funding-root", default=None,
                    help="默认 ${HERMES_DATA_DIR}/funding")
    ap.add_argument("--th", type=float, default=0.0003,
                    help="进场 |每小时 funding| 阈值")
    ap.add_argument("--hold-h", type=int, default=72,
                    help="持有小时数")
    ap.add_argument("--cost-bp", type=float, default=32.0)
    args = ap.parse_args()

    root = Path(args.funding_root) if args.funding_root else \
        Path(os.environ.get("HERMES_DATA_DIR", "/data")) / "funding"
    panel = load_panel(root)
    if not panel:
        print(f"no funding files under {root}")
        return 2

    print(f"panel coins {len(panel)} from {root}")
    print(f"params: th={args.th} hold={args.hold_h}h cost={args.cost_bp}bp")
    res = evaluate(panel, th=args.th, hold_h=args.hold_h,
                   cost_bp=args.cost_bp)
    print(f"entries {res['n_entries']} across {res['n_coins']} coins")
    _print_desc("coupon (funding)", res["coupon"])
    _print_desc("basis (premium)", res["basis"])
    _print_desc("total net", res["total"])
    print(f"leave-one-coin: {res['loo_pos']}/{res['loo_n']} still positive; "
          f"worst {res['loo_min'][1]:.1f} ({res['loo_min'][0]}), "
          f"best {res['loo_max'][1]:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
