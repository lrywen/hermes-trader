#!/usr/bin/env python3
"""WF-C 扫描覆盖：用真实 universe 快照做静态覆盖缺口 counterfactual（只读）。

背景（复核 2026-10-27）：
  * 生产扫描每周期硬顶 max_markets=60；crypto-only（默认 enable_crypto=1,
    enable_hip3=0）下预算 = 42 by-volume + 10 by-movers + 8 rotating sweep
    （universe_sweep=8，在 60 cap 内 carve）。
  * 审计引用过 "370 eligible、~72% 从未扫到"，但原始测算不在仓库；本机
    ~/.hermes/universe_cache/meta.json（2026-10-20 23:36Z）恰好含 **234 个
    perp 市场**——"52/234" 一类说法的真实分母即该时刻合格宇宙。
  * 没有任何落盘记录每周期实际扫了哪些币（session-log 只记 surfaced 币）。

本脚本严格复刻 perception.scan_once 的 crypto-only 选择逻辑（资格过滤 +
_pick_with_movers + 旋转 sweep 游标），在这份真实快照上：
  1. 给出单周期构成（vol/movers/sweep 各几个、实际总数）；
  2. 模拟连续 N 周期的 sweep 游标，量化多少周期覆盖全宇宙、稳态单周期覆盖率；
  3. 静态 top-volume 锁定导致的"长尾曝光"：一个币连续多少周期不在 60 池里；
  4. 成交额/OI 覆盖率（被扫池占全合格宇宙的比例）——说明漏的是"低量尾部"还是
     "有量但排名靠后"的市场。

价格用快照自带 midPx（无实时 WS mids，等同于全用未过期缓存口径；这是离线可得
的最接近生产的输入）。只读、不联网、不改任何生产代码。

用法：
  python scripts/wfc_scan_coverage_counterfactual.py
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

DEFAULT_META = os.path.expanduser("~/.hermes/universe_cache/meta.json")


def load_universe(path: str) -> list[dict[str, Any]]:
    """复刻 universe cache 的 meta+ctx 合并（coin=meta.name，数值字段在 ctx）。"""
    meta, ctx, _ = json.load(open(path))
    out = []
    for name, m in meta.items():
        c = ctx.get(name, {}) or {}
        row = {"coin": name, "type": m.get("type"), "dex": m.get("dex")}
        for k in ("dayNtlVlm", "prevDayPx", "markPx", "midPx",
                  "openInterest", "funding"):
            try:
                row[k] = float(c.get(k) or 0)
            except (TypeError, ValueError):
                row[k] = 0.0
        out.append(row)
    return out


def eligible_price(m: dict[str, Any]) -> float:
    return float(m.get("midPx") or m.get("markPx") or 0)


def abs_pct_24h(m: dict[str, Any]) -> float:
    prev, cur = m.get("prevDayPx", 0), eligible_price(m)
    if prev <= 0 or cur <= 0:
        return 0.0
    return abs((cur - prev) / prev * 100.0)


def pick_crypto_only(pool: list[dict[str, Any]], max_markets: int,
                     max_movers: int, sweep_n: int, sweep_reserve: int,
                     vol_floor: float, movers_min_pct: float,
                     offset: int) -> tuple[list[dict[str, Any]], int]:
    """与 perception crypto-only 路径一致；返回 (选中市场, 下一游标)。"""
    vol_budget = max(0, max_markets - max_movers - sweep_reserve)
    by_vol = sorted(pool, key=lambda m: m["dayNtlVlm"], reverse=True)
    chosen = by_vol[:vol_budget]
    have = {m["coin"] for m in chosen}
    cands = [m for m in pool if m["coin"] not in have
             and m["dayNtlVlm"] >= vol_floor]
    movers = [m for m in sorted(cands, key=abs_pct_24h, reverse=True)
              if abs_pct_24h(m) >= movers_min_pct][:max_movers]
    have.update(m["coin"] for m in movers)
    markets = chosen + movers

    ordered = sorted([m for m in pool if m["dayNtlVlm"] >= vol_floor],
                     key=lambda m: m["coin"])
    n = len(ordered)
    new_off = offset
    if sweep_n > 0 and n:
        room = max(0, max_markets - len(markets))
        take = min(sweep_n, room)
        added, j, steps = [], offset % n, 0
        while take > 0 and len(added) < take and steps < n:
            cand = ordered[j % n]
            if cand["coin"] not in have:
                added.append(cand)
                have.add(cand["coin"])
            j = (j + 1) % n
            steps += 1
        markets = markets + added
        new_off = 0 if steps >= n else j
    return markets, new_off


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta", default=DEFAULT_META)
    ap.add_argument("--max-markets", type=int, default=60)
    ap.add_argument("--max-movers", type=int, default=10)
    ap.add_argument("--sweep", type=int, default=8)
    ap.add_argument("--vol-floor", type=float, default=300_000.0)
    ap.add_argument("--movers-min-pct", type=float, default=1.0)
    ap.add_argument("--cycles", type=int, default=120)
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    uni = load_universe(args.meta)
    eligible = [m for m in uni if eligible_price(m) > 0
                and not m["coin"].startswith("@")
                and m.get("type") != "spot" and not m.get("dex")]
    N = len(eligible)

    # ── 单周期构成（游标 0 起步，等价于进程刚启动）──
    first, nxt = pick_crypto_only(
        eligible, args.max_markets, args.max_movers, args.sweep, args.sweep,
        args.vol_floor, args.movers_min_pct, 0)
    vol_coins = {m["coin"] for m in sorted(
        eligible, key=lambda m: m["dayNtlVlm"], reverse=True
    )[:max(0, args.max_markets - args.max_movers - args.sweep)]}
    first_vol = sum(1 for m in first if m["coin"] in vol_coins)
    first_sweep = [m["coin"] for m in first if m["coin"] not in vol_coins
                   and abs_pct_24h(m) < args.movers_min_pct + 1e-9][:8]

    # ── 连续周期 sweep 旋转，统计覆盖 ──
    ring = {m["coin"] for m in eligible if m["dayNtlVlm"] >= args.vol_floor}
    below_floor = N - len(ring)
    off = 0
    seen: set[str] = set()
    ring_cover_cycle = None
    per_cycle_counts = []
    gap_streak: dict[str, int] = {}
    max_gap: dict[str, int] = {}
    for cyc in range(args.cycles):
        mk, off = pick_crypto_only(
            eligible, args.max_markets, args.max_movers, args.sweep,
            args.sweep, args.vol_floor, args.movers_min_pct, off)
        got = {m["coin"] for m in mk}
        per_cycle_counts.append(len(got))
        for c in {m["coin"] for m in eligible}:
            if c in got:
                gap_streak[c] = 0
            else:
                gap_streak[c] = gap_streak.get(c, 0) + 1
                max_gap[c] = max(max_gap.get(c, 0), gap_streak[c])
        seen |= got
        if ring_cover_cycle is None and ring.issubset(seen):
            ring_cover_cycle = cyc + 1

    # ── 成交额/OI 覆盖 ──
    tot_vlm = sum(m["dayNtlVlm"] for m in eligible)
    tot_oi = sum(m["openInterest"] for m in eligible)
    first_vlm = sum(m["dayNtlVlm"] for m in first)
    first_oi = sum(m["openInterest"] for m in first)
    tail = [m for m in eligible if m["coin"] not in {x["coin"] for x in first}]
    tail_above_1m = [m for m in tail if m["dayNtlVlm"] >= 1e6]

    # ── ring 内长尾最大连续漏扫周期（低于地板、按设计永不扫描的 133 个排除，
    # 否则会把"设计性排除"误报成"漏扫"）。.get 默认 0：每周期都被扫的固定
    # top-vol 从不进入 max_gap，其 gap 本就是 0，绝不能回退成 cycles。──
    gaps = sorted(((max_gap.get(c, 0), c) for c in ring),
                  reverse=True)

    report = {
        "snapshot": args.meta,
        "eligible_crypto_perps": N,
        "budget": {"max_markets": args.max_markets, "movers": args.max_movers,
                   "sweep": args.sweep},
        "cycle_1": {"scanned": len(first), "by_volume": first_vol,
                    "sweep_examples": first_sweep},
        "steady_state": {
            "scanned_per_cycle": sorted(set(per_cycle_counts)),
            "single_cycle_coverage_pct": round(
                100.0 * per_cycle_counts[-1] / N, 1),
            # sweep 只旋转成交额 >= vol_floor 的 ring；低于地板的 133 个市场按
            # 设计永不进入扫描（pico-cap 噪声）。两个口径分开报，避免把"按设计
            # 排除"误读为"漏扫"。
            "sweep_ring_size": len(ring),
            "below_floor_never_swept": below_floor,
            "cycles_to_cover_ring": ring_cover_cycle,
            "expected_ring_pass_cycles": (
                (len(ring) + args.sweep - 1) // args.sweep),
        },
        "notional_coverage_cycle1_pct": round(100.0 * first_vlm / tot_vlm, 1),
        "oi_coverage_cycle1_pct": round(100.0 * first_oi / tot_oi, 1),
        "tail_excluded_cycle1": len(tail),
        "tail_with_vol_ge_1m": len(tail_above_1m),
        "tail_ge_1m_examples": [
            {"coin": m["coin"], "dayNtlVlm_usd": round(m["dayNtlVlm"]),
             "pct24h": round(abs_pct_24h(m), 2)}
            for m in sorted(tail_above_1m,
                            key=lambda m: m["dayNtlVlm"], reverse=True)[:10]],
        "worst_sweep_gap_cycles_top10": gaps[:10],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save:
        json.dump(report, open(args.save, "w"), ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
