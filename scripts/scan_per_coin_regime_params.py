#!/usr/bin/env python3
"""离线参数扫描：在已打分的 per-coin regime 补样数据上复算不同 demote 规则的判别力。

输入是 backfill_per_coin_regime_shadow.py 产出、再经 reconcile_per_coin_regime_shadow.py
--write 打分后的 jsonl（每行含 detail 特征 + pnl_pct）。本脚本只做纯计算，不拉 K 线。

只有 macro_aligned=True 的记录参与扫描 —— demote 规则的语义就是"宏观顺势但自身逆势时降级"，
非 aligned 记录本来就走不到这条分支，也没有被 reconcile 打分。

用法:
    python3 scripts/scan_per_coin_regime_params.py --file /data/per_coin_regime_backfill.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from typing import Any, Callable, Iterable, Optional

# 一条规则要进入"候选可用"名单，demote 组至少要有这么多样本。
MIN_DEMOTE_N = 30
# 置换检验次数。
PERM_ITERS = 20000


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(path: str) -> list[dict[str, Any]]:
    """读入补样文件，只保留 macro_aligned 且已打分的记录。"""
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not rec.get("macro_aligned"):
                continue
            pnl = _f(rec.get("pnl_pct"))
            if pnl is None:
                continue
            det = rec.get("detail") or {}
            side = str(rec.get("side") or "").lower()
            if side not in ("long", "short"):
                continue
            gap = _f(det.get("own_gap_pct"))
            out.append({
                "coin": rec.get("coin"),
                "side": side,
                "pnl": pnl,
                "gap": gap,
                "adx4h": _f(det.get("adx4h")),
                "own1h": str(det.get("own_1h_regime") or "neutral"),
                "own1h_score": _f(det.get("own_1h_score")),
                "macro_score": _f(det.get("macro_trend_score")),
            })
    return out


def _against_4h(r: dict[str, Any]) -> bool:
    """4h 收盘相对 EMA21 的方向与开仓方向相反。"""
    gap = r["gap"]
    if gap is None:
        return False
    return gap < 0 if r["side"] == "long" else gap > 0


def _against_1h(r: dict[str, Any]) -> bool:
    """自身 1h regime 与开仓方向相反。"""
    return r["own1h"] == ("down" if r["side"] == "long" else "up")


def _mag(r: dict[str, Any]) -> float:
    gap = r["gap"]
    return abs(gap) if gap is not None else 0.0


def build_rules() -> list[tuple[str, Callable[[dict[str, Any]], bool]]]:
    """枚举候选 demote 规则。返回 (名称, 判定函数) 列表。"""
    rules: list[tuple[str, Callable[[dict[str, Any]], bool]]] = []

    # A. 现行口径：4h 逆势 + ADX 确认强度。adx=20 即线上默认。
    for adx in (0.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0):
        rules.append((
            f"4h_against & adx4h>={adx:g}",
            lambda r, a=adx: _against_4h(r) and (r["adx4h"] or 0.0) >= a,
        ))

    # B. 4h 逆势 + 偏离幅度门槛（过滤"刚跌破 EMA 一点点"的噪声）。
    for g in (0.5, 1.0, 2.0, 3.0, 5.0):
        rules.append((
            f"4h_against & |gap|>={g:g}%",
            lambda r, x=g: _against_4h(r) and _mag(r) >= x,
        ))

    # C. 改用 1h 自身 regime 替代 4h。
    rules.append(("1h_against", _against_1h))
    for s in (0.4, 0.5, 0.55, 0.6, 0.65, 0.7):
        rules.append((
            f"1h_against & own1h_score>={s:g}",
            lambda r, x=s: _against_1h(r) and (r["own1h_score"] or 0.0) >= x,
        ))

    # D. 1h 与 4h 的交并。
    rules.append(("1h_against | 4h_against", lambda r: _against_1h(r) or _against_4h(r)))
    rules.append(("1h_against & 4h_against", lambda r: _against_1h(r) and _against_4h(r)))

    # E. 三者组合：4h 逆势 + ADX + 幅度。
    for adx in (15.0, 20.0, 25.0):
        for g in (1.0, 2.0, 3.0):
            rules.append((
                f"4h_against & adx4h>={adx:g} & |gap|>={g:g}%",
                lambda r, a=adx, x=g: (
                    _against_4h(r) and (r["adx4h"] or 0.0) >= a and _mag(r) >= x),
            ))

    # F. 只看自身非顺势（含 neutral/chop），比 against 宽。
    rules.append((
        "1h_not_aligned",
        lambda r: r["own1h"] != ("up" if r["side"] == "long" else "down"),
    ))

    # G. 宏观强度弱 + 自身逆势（宏观越弱越该听自身的）。
    for ms in (0.6, 0.7, 0.8):
        rules.append((
            f"4h_against & macro_score<{ms:g}",
            lambda r, x=ms: _against_4h(r) and (r["macro_score"] or 1.0) < x,
        ))

    return rules


def _stats(vals: list[float]) -> tuple[int, float, float, float]:
    n = len(vals)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    win = 100.0 * sum(1 for v in vals if v > 0) / n
    return n, win, statistics.fmean(vals), statistics.median(vals)


def _perm_p(a: list[float], b: list[float], iters: int, rng: random.Random) -> float:
    """双尾置换检验：两组均值差在随机重分组下出现同等极端的概率。"""
    if not a or not b:
        return 1.0
    obs = abs(statistics.fmean(a) - statistics.fmean(b))
    pool = a + b
    na = len(a)
    hits = 0
    for _ in range(iters):
        rng.shuffle(pool)
        d = abs(statistics.fmean(pool[:na]) - statistics.fmean(pool[na:]))
        if d >= obs:
            hits += 1
    return (hits + 1) / (iters + 1)


def evaluate(rows: Iterable[dict[str, Any]], rule: Callable[[dict[str, Any]], bool],
             rng: random.Random, iters: int) -> dict[str, Any]:
    demote = [r["pnl"] for r in rows if rule(r)]
    keep = [r["pnl"] for r in rows if not rule(r)]
    dn, dwin, dmean, dmed = _stats(demote)
    kn, kwin, kmean, kmed = _stats(keep)
    p = _perm_p(list(demote), list(keep), iters, rng) if dn >= 5 and kn >= 5 else 1.0
    return {
        "dn": dn, "dwin": dwin, "dmean": dmean, "dmed": dmed,
        "kn": kn, "kwin": kwin, "kmean": kmean, "kmed": kmed,
        "diff": dmean - kmean, "win_diff": dwin - kwin, "p": p,
    }


def _print_table(title: str, rows: list[dict[str, Any]],
                 rules: list[tuple[str, Callable[[dict[str, Any]], bool]]],
                 rng: random.Random, iters: int) -> None:
    print()
    print("=" * 108)
    print(f"{title}   (n={len(rows)})")
    print("=" * 108)
    if not rows:
        print("  no samples")
        return
    header = ("%-44s %5s %6s %8s | %5s %6s %8s | %8s %7s %7s"
              % ("rule", "n_dem", "win%", "mean%", "n_keep", "win%", "mean%",
                 "EVdiff", "Δwin", "p"))
    print(header)
    print("-" * 108)
    results = []
    for name, fn in rules:
        res = evaluate(rows, fn, rng, iters)
        results.append((name, res))
        print("%-44s %5d %6.1f %8.3f | %5d %6.1f %8.3f | %+8.3f %+7.1f %7.3f"
              % (name, res["dn"], res["dwin"], res["dmean"],
                 res["kn"], res["kwin"], res["kmean"],
                 res["diff"], res["win_diff"], res["p"]))

    # demote 组应当比 keep 组更差（这才叫"该降级"），所以按 diff 升序挑最负的。
    usable = [(n, r) for n, r in results if r["dn"] >= MIN_DEMOTE_N and r["kn"] >= MIN_DEMOTE_N]
    usable.sort(key=lambda kv: kv[1]["diff"])
    print("-" * 108)
    print(f"top separators (n_demote>={MIN_DEMOTE_N}, demote 组 EV 越低越好):")
    if not usable:
        print("  none — 所有规则的 demote 组样本量都不足")
        return
    for name, r in usable[:5]:
        print("  %-44s EVdiff=%+.3f pct  Δwin=%+.1f pct  p=%.3f  (n=%d)"
              % (name, r["diff"], r["win_diff"], r["p"], r["dn"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="/data/per_coin_regime_backfill.jsonl")
    ap.add_argument("--iters", type=int, default=PERM_ITERS)
    ap.add_argument("--seed", type=int, default=20260911)
    args = ap.parse_args()

    rows = load_rows(args.file)
    if not rows:
        print("no scored aligned rows found in", args.file)
        return 1

    rng = random.Random(args.seed)
    rules = build_rules()

    _print_table("ALL (macro-aligned, scored)", rows, rules, rng, args.iters)
    _print_table("LONG only", [r for r in rows if r["side"] == "long"],
                 rules, rng, args.iters)
    _print_table("SHORT only", [r for r in rows if r["side"] == "short"],
                 rules, rng, args.iters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
