#!/usr/bin/env python3
"""C-1: regime-gate historical replay using realized shadow-book closes.

Classifies each CLOSED taker position by whether its side agreed with the
entry_regime recorded at open, then compares realized win rate / spot% / ROE% /
USD between the COUNTER-regime set (the set a hard regime-direction gate would
remove) and the ALIGNED set. Bootstrap 95% CI on the differences. Read-only.

Counter-regime definition:
  long & entry_regime == 'down'   OR   short & entry_regime == 'up'
Aligned:
  long & entry_regime == 'up'     OR   short & entry_regime == 'down'
neutral / chop are non-directional and reported separately, not compared.
"""
from __future__ import annotations

import json
import random

BOOK = "/data/.shadow-book.json"


def classify(side: str, reg: str) -> str:
    if side == "long" and reg == "down":
        return "counter"
    if side == "short" and reg == "up":
        return "counter"
    if side == "long" and reg == "up":
        return "aligned"
    if side == "short" and reg == "down":
        return "aligned"
    return reg if reg in ("neutral", "chop") else "other"


def wr(rows):
    return sum(1 for r in rows if r["usd"] > 0) / len(rows)


def mean(rows, key):
    return sum(r[key] for r in rows) / len(rows)


def boot(a, b, fn, n=4000, seed=11):
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        x = fn([a[rng.randrange(len(a))] for _ in a])
        y = fn([b[rng.randrange(len(b))] for _ in b])
        diffs.append(x - y)
    diffs.sort()
    return diffs[int(0.025 * n)], diffs[int(0.975 * n)]


def main() -> int:
    d = json.load(open(BOOK))
    closes = [f for f in d["accounts"]["taker"]["fills"] if f["type"] == "close"]
    rows = []
    for c in closes:
        rows.append({
            "coin": c["coin"], "side": c["side"], "reg": c.get("entry_regime"),
            "grp": classify(c["side"], str(c.get("entry_regime"))),
            "spot": c.get("spot_pct") or 0.0,
            "roe": c.get("realized_pnl_pct") or 0.0,
            "usd": c.get("realized_pnl_usd") or 0.0,
            "reason": c.get("reason", ""),
        })
    counter = [r for r in rows if r["grp"] == "counter"]
    aligned = [r for r in rows if r["grp"] == "aligned"]
    neutral = [r for r in rows if r["grp"] in ("neutral", "chop", "other")]

    print("=" * 96)
    print("C-1 regime 闸门历史回放：逆势单 vs 顺势单（已实现结果）")
    print("=" * 96)
    print(f"逆势 n={len(counter)} | 顺势 n={len(aligned)} | 中性/震荡 n={len(neutral)}")

    for label, rs in (("逆势(将被硬闸门移除)", counter), ("顺势", aligned),
                      ("中性/震荡", neutral)):
        if not rs:
            print(f"\n{label}: 无样本")
            continue
        tot = sum(r["usd"] for r in rs)
        print(f"\n{label}  n={len(rs)}")
        print(f"  胜率 {wr(rs):.1%} | spot 均值 {mean(rs,'spot'):+.2f}% | "
              f"ROE 均值 {mean(rs,'roe'):+.2f}% | USD 合计 {tot:+.2f} "
              f"| USD/笔 {tot/len(rs):+.3f}")

    if counter and aligned:
        print("\n" + "-" * 96)
        wr_lo, wr_hi = boot(counter, aligned, wr)
        sp_lo, sp_hi = boot(counter, aligned, lambda r: mean(r, "spot"))
        roe_lo, roe_hi = boot(counter, aligned, lambda r: mean(r, "roe"))
        print(f"胜率差(逆势-顺势) {wr(counter)-wr(aligned):+.1%}  "
              f"95%CI [{wr_lo:+.1%},{wr_hi:+.1%}]")
        print(f"spot差 {mean(counter,'spot')-mean(aligned,'spot'):+.2f}%  "
              f"95%CI [{sp_lo:+.2f},{sp_hi:+.2f}]")
        print(f"ROE差 {mean(counter,'roe')-mean(aligned,'roe'):+.2f}%  "
              f"95%CI [{roe_lo:+.2f},{roe_hi:+.2f}]")

    print("\n逆势单明细：")
    for r in counter:
        print(f"  {r['coin']:10s} {r['side']:5s} reg={r['reg']:8s} "
              f"spot={r['spot']:+6.2f}% roe={r['roe']:+7.2f}% usd={r['usd']:+7.3f}")

    print("\n判读：若逆势 n>=20 且 胜率/收益差值 CI 完全<0(不含0) → 硬闸门有据；"
          "若 CI 含0 或 n 很小 → 不足以硬拦，维持观察并扩样。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
