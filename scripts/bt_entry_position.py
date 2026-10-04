#!/usr/bin/env python3
"""入场位置改造的历史回测（candidate-entry replay）。

目的
----
验证 late_chase Leg3 锁存迟滞 + Leg4 绝对区间分位硬闸，能否在不牺牲机会的
前提下，剔除"追在垂直行情末端（多头顶部分位/空头底部分位）"的亏损入场。

方法
----
1. 候选样本：G2 logs/b3_81coin_filt_ra.jsonl 中 arm=baseline & fired 的入场
   （35k+，2026-03-20~09-20，185 天，81 币）。每条携带真实 OOS 结果
   pnl_gross/net（bp）、peak_pct。
2. 对每个入场时刻，只用**入场当时已收盘**的 5m/15m K线重算：
     - 5m 实时（以入场价替换当根收盘）RSI / 相对EMA21的 ATR 扩展；
     - 入场价在近 96 根 15m（24h）区间的分位。
   不使用任何入场后数据来决定过滤（结果字段只用于事后打分）。
3. 三种策略并行裁决同一条候选：
     NONE  不过滤（baseline，全收）；
     OLD   旧规则——单点 5m RSI>80 或 ext>3ATR 才拦，下一 tick 立即解禁；
     NEW   新规则——触及极端则锁存（RSI 回到60/40 或冷却15m 才释放），
           且多头>=90分位/空头<=10分位直接拦。
4. 对比三策略 admitted 集合的笔数、总/均 net bp、胜率、以及被拦样本的真实
   平均 net（若被拦样本平均为负，说明拦对了）。

K线来自 Binance Vision（data-api.binance.vision，支持历史 startTime），按
(币,日) 落盘缓存到 logs/bt_entry_pos_cache/。纯标准库 + 本仓指标。
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.indicators.math import atr as atr_arr
from hermes_trader.indicators.math import ema as ema_arr
from hermes_trader.indicators.math import rsi as rsi_arr

CACHE_DIR = _REPO / "logs" / "bt_entry_pos_cache"

# ── 策略参数（与生产 defaults 对齐）────────────────────────────────────────
RT_RSI_HIGH, RT_RSI_LOW, RT_EXT = 80.0, 20.0, 3.0
REL_RSI_HIGH, REL_RSI_LOW = 60.0, 40.0
COOLDOWN_MS = 15 * 60_000
RP_BARS, RP_LONG_HI, RP_SHORT_LO = 96, 90.0, 10.0

_MS_5M, _MS_15M = 300_000, 900_000


# ── K线抓取/缓存 ────────────────────────────────────────────────────────────

def _bin_symbol(coin: str) -> str:
    return {"kBONK": "BONK"}.get(coin, coin) + "USDT"


def fetch_day(coin: str, day_start_ms: int, interval_ms: int) -> list:
    """抓取某 UTC 日（含余量）的 K线，磁盘缓存。返回 [t,o,h,l,c]。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    iv = interval_ms // 60_000
    fn = CACHE_DIR / f"{coin}_{day_start_ms}_{iv}.jsonl.gz"
    if fn.exists():
        try:
            with gzip.open(fn, "rt") as f:
                return [json.loads(x) for x in f]
        except Exception:
            fn.unlink(missing_ok=True)
    sym = _bin_symbol(coin)
    qs = (f"symbol={sym}&interval={iv}m"
          f"&startTime={day_start_ms - interval_ms}"
          f"&endTime={day_start_ms + 86_400_000 + interval_ms}&limit=1000")
    url = "https://data-api.binance.vision/api/v3/klines?" + qs
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=12) as r:
                raw = json.loads(r.read())
            out = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                    float(x[4])] for x in raw]
            with gzip.open(fn, "wt") as f:
                for row in out:
                    f.write(json.dumps(row) + "\n")
            return out
        except Exception as e:
            err = e
            time.sleep(0.4 * (attempt + 1))
    # 缺数据：写空标记，避免反复重试当天不存在的盘口
    with gzip.open(fn, "wt") as f:
        f.write("")
    return []


def prefetch(cands: list[dict], workers: int = 16) -> None:
    """并行抓取所有候选需要的 (coin, 日, 5m/15m) K线，供重放本地读取。"""
    from concurrent.futures import ThreadPoolExecutor

    jobs: set[tuple[str, int, int]] = set()
    for r in cands:
        day = (int(r["entry_t"]) // 86_400_000) * 86_400_000
        # 当天 + 前一天（candles_upto 会拼接两天）
        for d in (day - 86_400_000, day):
            jobs.add((r["coin"], d, _MS_5M))
            jobs.add((r["coin"], d, _MS_15M))

    # 只保留本地还没有的
    todo = []
    for coin, d, iv in jobs:
        fn = CACHE_DIR / f"{coin}_{d}_{iv // 60_000}.jsonl.gz"
        if not fn.exists():
            todo.append((coin, d, iv))
    print(f"需抓取 {len(todo)}/{len(jobs)} 个 (币,日,周期)，{workers} 并发…")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_day, c, d, iv) for c, d, iv in todo]
        for _ in futs:
            _.result()
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{len(todo)}")
    print("K线就绪。")



_DAY_CACHE: dict[tuple, list] = {}


def candles_upto(coin: str, t_ms: int, interval_ms: int) -> list:
    """覆盖 t_ms 前所需的最近约 N 根 K线（取当天+前一天拼接，截已收盘）。"""
    day0 = (t_ms // 86_400_000) * 86_400_000
    key = (coin, day0, interval_ms)
    if key not in _DAY_CACHE:
        a = fetch_day(coin, day0 - 86_400_000, interval_ms)
        b = fetch_day(coin, day0, interval_ms)
        seen: dict[int, list] = {}
        for row in a + b:
            seen[row[0]] = row
        _DAY_CACHE[key] = [seen[k] for k in sorted(seen)]
    rows = _DAY_CACHE[key]
    return [r for r in rows if r[0] + interval_ms <= t_ms]


# ── 指标 ────────────────────────────────────────────────────────────────────

class _C:  # 轻量 candle 容器供本仓 atr 使用
    __slots__ = ("o", "h", "l", "c")
    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c


def rt_terminal(coin: str, t_ms: int, price: float) -> tuple[float, float] | None:
    """5m 实时 RSI 与 ext(ATR)：以入场价替换当根（未收盘）收盘。"""
    closed = candles_upto(coin, t_ms, _MS_5M)
    if len(closed) < 30:
        return None
    cs = [_C(r[1], r[2], r[3], r[4]) for r in closed]
    # 合成当根
    cs.append(_C(price, price, price, price))
    closes = [c.c for c in cs]
    rsi = rsi_arr(cs, 14)[-1]
    e21 = ema_arr(closes, 21)[-1]
    a = atr_arr(cs, 14)[-1]
    if not all(math.isfinite(x) for x in (rsi, e21)) or not (a > 0):
        return None
    return float(rsi), float((price - e21) / a)


def range_pct(coin: str, t_ms: int, price: float) -> float | None:
    closed = candles_upto(coin, t_ms, _MS_15M)
    win = closed[-RP_BARS:]
    if len(win) < 10:
        return None
    hi, lo = max(r[2] for r in win), min(r[3] for r in win)
    if hi <= lo:
        return None
    return (price - lo) / (hi - lo) * 100.0


# ── 主流程 ──────────────────────────────────────────────────────────────────

def load_candidates(path: str, max_per_day: int) -> list[dict]:
    by_day: dict[int, list[dict]] = {}
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get("type") != "trade" or r.get("arm") != "baseline":
                continue
            if not r.get("fired"):
                continue
            day = (int(r["entry_t"]) // 86_400_000) * 86_400_000
            by_day.setdefault(day, []).append(r)
    out: list[dict] = []
    for day in sorted(by_day):
        grp = by_day[day]
        if max_per_day and len(grp) > max_per_day:
            step = len(grp) / max_per_day
            grp = [grp[int(i * step)] for i in range(max_per_day)]
        out.extend(grp)
    out.sort(key=lambda r: int(r["entry_t"]))
    return out


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    nets = [float(r["pnl_net"]) for r in rows]
    return {
        "n": len(nets),
        "sum": sum(nets),
        "mean": sum(nets) / len(nets),
        "win": sum(1 for x in nets if x > 0) / len(nets),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates",
                    default=str(_REPO / "logs/b3_81coin_filt_ra.jsonl"))
    ap.add_argument("--max-per-day", type=int, default=40,
                    help="每日均匀抽样候选上限（控制K线抓取量），0=全量")
    ap.add_argument("--days", type=int, default=0,
                    help="只跑最近N个 UTC 日（0=全部）")
    ap.add_argument("--workers", type=int, default=16,
                    help="K线预取并发数")
    ap.add_argument("--skip-prefetch", action="store_true",
                    help="跳过预取（假定本地缓存已齐）")
    args = ap.parse_args()

    cands = load_candidates(args.candidates, args.max_per_day)
    if args.days:
        keep = {(int(r["entry_t"]) // 86_400_000) for r in cands}
        keep = sorted(keep)[-args.days:]
        cands = [r for r in cands
                 if (int(r["entry_t"]) // 86_400_000) in set(keep)]
    print(f"候选 {len(cands)} 笔。")
    if not args.skip_prefetch:
        prefetch(cands, workers=args.workers)
    print("开始重放…")

    # NEW 策略的锁存：coin|dir -> since_ms（跨候选按时间推进）
    latch: dict[tuple, int] = {}

    none_rows, old_rows, new_rows = [], [], []
    new_blocked, old_blocked = [], []

    t0 = time.time()
    for i, r in enumerate(cands):
        coin, side = r["coin"], r["side"]
        t_ms, price = int(r["entry_t"]), float(r["entry_px"])
        none_rows.append(r)

        term = rt_terminal(coin, t_ms, price)
        pct = range_pct(coin, t_ms, price)
        d = "up" if side == "long" else "down"
        is_term = False
        if term is not None:
            rsi, ext = term
            if side == "long":
                is_term = rsi > RT_RSI_HIGH or ext > RT_EXT
            else:
                is_term = rsi < RT_RSI_LOW or ext < -RT_EXT

        # ── OLD：单点，当根极端才拦（无记忆）──
        old_block = is_term
        (old_blocked if old_block else old_rows).append(r)

        # ── NEW：锁存迟滞 + 区间分位 ──
        key = (coin, d)
        since = latch.get(key)
        if is_term:
            if since is None:
                latch[key] = t_ms
        elif since is not None:
            cooled = t_ms - since >= COOLDOWN_MS
            neutral = (pct is None) or (
                side == "long" and term is not None and term[0] <= REL_RSI_HIGH) \
                or (side == "short" and term is not None and term[0] >= REL_RSI_LOW)
            if cooled or neutral:
                latch.pop(key, None)
                since = None
        new_latched = key in latch

        rp_block = False
        if pct is not None:
            if side == "long":
                rp_block = pct >= RP_LONG_HI
            else:
                rp_block = pct <= RP_SHORT_LO

        new_block = new_latched or rp_block
        (new_blocked if new_block else new_rows).append(r)

        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(cands)}  ({time.time()-t0:.0f}s)")

    print("\n===== 结果（net bp）=====")
    for name, rows in (("NONE", none_rows), ("OLD", old_rows),
                       ("NEW", new_rows)):
        s = _stats(rows)
        print(f"{name:4} admitted n={s['n']:6} sum={s['sum']:9.0f} "
              f"mean={s['mean']:7.2f} win={s['win']:.3f}")
    for name, rows in (("OLD-blocked", old_blocked), ("NEW-blocked", new_blocked)):
        s = _stats(rows)
        if s["n"]:
            print(f"{name:11} n={s['n']:6} meanNet={s['mean']:7.2f} "
                  f"win={s['win']:.3f} (负=拦对)")

    # NEW 相对 NONE 的增量（被拦部分的净贡献）
    s_none, s_new = _stats(none_rows), _stats(new_rows)
    delta = s_new["sum"] - s_none["sum"]
    print(f"\nNEW 相对 NONE：总净 bp 变化 {delta:+.0f}，"
          f"保留 {s_new['n']/s_none['n']*100:.1f}% 的入场")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
