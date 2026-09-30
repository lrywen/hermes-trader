#!/usr/bin/env python3
"""M-2 Binance 逐笔 tape CVD 预注册回放（一次性、严格样本外）。

口径冻结于 docs/research/2026-09/m2_binance_tape_cvd_prereg_2026-09-28.md。
本脚本不做参数搜索；只实现冻结的单一主配置（H1）+ 文档规定的稳健性格子。

数据：/mnt/tick/raw/parquet/{BTCUSDT,ETHUSDT}/*.parquet
  列 trade_id,price,qty,quote_qty,time,is_buyer_maker

主配置（H1）：
  signed flow s = +quote_qty (is_buyer_maker=False) / -quote_qty (True)
  W=60s 窗口 CVD；在 5m UTC 网格取值
  z = CVD_W / std(过去24h 同网格点 CVD_W，288点)
  入场分位用各币过去30自然日同网格 z 的经验 q10/q90
  z>=q90 做多 / z<=q10 做空；持有 h=5m；步进=持有期（去重叠）
  成本主口径 COST_RT=11bp；另报 9bp / 15bp
时间切分：前50%天 train（仅定口径），后50%天 test 为唯一报告区间。
推断：净收益按入场 UTC 天聚合 -> 日均值 bps；5天移动块 block bootstrap
  B=10000 seed=20260928，报告 95% CI。
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import random
import re
import statistics
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import numpy as np

TICK_ROOT = Path("/mnt/tick/raw/parquet")
COINS = ["BTCUSDT", "ETHUSDT"]

GRID_MS = 5 * 60_000          # 5m 网格 / 主 horizon
WINDOW_MS = 60_000            # CVD W=60s
Z_LOOKBACK = 288             # 过去24h 的 5m 网格点
QUANTILE_DAYS = 30           # 入场分位过去30自然日
TRAIN_FRAC = 0.5
COST_MAIN = 0.0011           # 11bp round-trip 主口径
COST_SENS = [0.0009, 0.0015]
BOOT_B = 10_000
BOOT_SEED = 20260928
BLOCK_DAYS = 5

DAY_MS = 86_400_000


# ---------------------------------------------------------------- 装载

def list_days(coin: str) -> list[str]:
    files = glob.glob(str(TICK_ROOT / coin / "*.parquet"))
    days = sorted(
        re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f)).group(1)
        for f in files
    )
    return days


def _day_bounds(day: str) -> tuple[int, int]:
    d0 = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return d0, d0 + DAY_MS


def build_coin_series(coin: str, days: list[str]):
    """逐日流式聚合到 5m 网格，内存只保留网格数组（不载入全部逐笔）。

    结果按 (coin, 首末日, 网格数) 缓存到 /tmp/m2_cache，重跑/中断后续跑免重建。
    返回 dict:
      grid: 升序网格时间 ms（每根 5m，覆盖 span）
      cvd : 每根网格闭合前 60s 窗口 signed flow 和
      px  : 每根网格时刻 <=t 的最后一笔价（P_t）
    60s 窗口仅在每天首根网格（00:00）会跨入前一天 23:59:00，用
    prev_tail（前一天最后 60s 的 time/signed 序列）补齐。
    """
    n_grids_per_day = DAY_MS // GRID_MS  # 288
    total_grids = n_grids_per_day * len(days)
    cache_dir = Path("/tmp/m2_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cfile = cache_dir / f"{coin}_{days[0]}_{days[-1]}_{total_grids}.npz"
    if cfile.exists():
        z = np.load(cfile)
        return {"grid": list(z["grid"]), "cvd": z["cvd"], "px": z["px"]}

    d_first, _ = _day_bounds(days[0])
    grids = [d_first + i * GRID_MS for i in range(total_grids)]
    cvd = np.zeros(total_grids, dtype="float64")
    px = np.full(total_grids, math.nan, dtype="float64")

    prev_tail_t = np.empty(0, dtype="int64")
    prev_tail_s = np.empty(0, dtype="float64")

    for di, day in enumerate(days):
        f = TICK_ROOT / coin / f"{coin}-trades-{day}.parquet"
        tbl = pq.read_table(f, columns=["quote_qty", "time", "price", "is_buyer_maker"])
        qq = tbl.column("quote_qty").to_numpy(zero_copy_only=False).astype("float64")
        tm = tbl.column("time").to_numpy(zero_copy_only=False).astype("int64")
        # 单位归一：2026-06-29..08-11 的原始数据 time 为微秒(16位)，统一为毫秒
        if tm.size and len(str(int(tm[len(tm) // 2]))) >= 16:
            tm = tm // 1000
        pr = tbl.column("price").to_numpy(zero_copy_only=False).astype("float64")
        ibm = tbl.column("is_buyer_maker").to_numpy(zero_copy_only=False).astype("bool")

        d0, d1 = _day_bounds(day)
        base = di * n_grids_per_day

        # signed flow
        signed = qq.copy()
        signed[ibm] = -signed[ibm]

        # px：每笔所属 5m 网格内「最后出现」的价（按文件顺序，通常升序）
        gi = ((tm - d0) // GRID_MS).astype("int64")
        valid = (gi >= 0) & (gi < n_grids_per_day)
        if not np.any(valid):
            prev_tail_t = np.empty(0, dtype="int64")
            prev_tail_s = np.empty(0, dtype="float64")
            continue
        # px：Binance 官方导出按 time 升序，直接按行顺序写所属网格，
        # 同网格后写覆盖（即该网格最后一笔），省掉大规模 lexsort。
        px[base + gi[valid]] = pr[valid]

        # CVD：逐笔 t 贡献给网格端点 g = ceil(t/grid)*grid（t为网格点时g=t）
        rem = tm % GRID_MS
        g_end = tm + np.where(rem == 0, 0, GRID_MS - rem)  # 单个目标网格
        # 60s 窗口 < 5m，故每笔只落在 1 个网格端点（g_end），g_end < t+60s 恒成立
        gegi = (g_end - d0) // GRID_MS
        m = (gegi >= 0) & (gegi < n_grids_per_day)
        np.add.at(cvd, base + gegi[m], signed[m])

        # 当天尾部 60s（供次日首根网格）——直接从数组切
        tmask = tm >= d1 - WINDOW_MS
        prev_for_next_t = tm[tmask]
        prev_for_next_s = signed[tmask]

        # 首根网格补前一天尾部 (d0-60s, d0]
        if len(prev_tail_t):
            ok = prev_tail_t > d0 - WINDOW_MS
            cvd[base] += float(np.sum(prev_tail_s[ok]))

        prev_tail_t = prev_for_next_t
        prev_tail_s = prev_for_next_s

    np.savez(cfile, grid=np.array(grids, dtype="int64"), cvd=cvd, px=px)
    return {"grid": grids, "cvd": cvd, "px": px}


# ---------------------------------------------------------------- 信号

def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return math.nan
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    w = pos - lo
    return sorted_vals[lo] * (1 - w) + sorted_vals[hi] * w


def generate_trades(coin: str, days: list[str], split_day: str):
    """按冻结口径产出 test 区间交易。返回 list[dict]。

    每条: coin, entry_grid, day(UTC 'YYYY-MM-DD'), side(+1/-1),
          gross(未扣成本小数收益), z
    """
    ser = build_coin_series(coin, days)
    grids = ser["grid"]
    cvd = ser["cvd"]
    px = ser["px"]
    n = len(grids)

    # z 标准化：std 用过去 288 个 cvd（不含当根，防用当根自身）
    z = [math.nan] * n
    run: list[float] = []
    for i in range(n):
        if i >= Z_LOOKBACK:
            window = cvd[i - Z_LOOKBACK:i]
            sd = statistics.pstdev(window)
            z[i] = cvd[i] / sd if sd > 0 else math.nan
        run.append(cvd[i])

    split_ms = int(datetime.fromisoformat(split_day)
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    trades = []
    # 过去30自然日 z 收集（按自然日索引，因果）
    # day_ms -> list z in that day, 仅在处理到时点之前累计
    from collections import defaultdict
    z_by_day: dict[int, list[float]] = defaultdict(list)

    for i in range(n - 1):
        g = grids[i]
        day_idx = g // DAY_MS

        # 分位只用过去30天（不含当天）的 z
        lo_day = day_idx - QUANTILE_DAYS
        pool = []
        for dd in range(lo_day, day_idx):
            pool.extend(z_by_day.get(dd, ()))
        # 先把当根 z 暂不纳入自身分位；计算完入场后再归档
        zi = z[i]
        if math.isnan(zi) or not pool or g < split_ms:
            z_by_day[day_idx].append(zi)
            continue

        sp = sorted(pool)
        q10 = quantile(sp, 0.10)
        q90 = quantile(sp, 0.90)

        side = 0
        if zi >= q90:
            side = +1
        elif zi <= q10:
            side = -1

        p0 = px[i]
        p1 = px[i + 1]
        if side != 0 and not (math.isnan(p0) or math.isnan(p1)) and p0 > 0:
            gross = side * (p1 - p0) / p0
            dstr = datetime.fromtimestamp(g / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            trades.append({
                "coin": coin, "entry_grid": g, "day": dstr,
                "day_idx": day_idx, "side": side, "gross": gross, "z": zi,
            })
        z_by_day[day_idx].append(zi)

    return trades


# ---------------------------------------------------------------- bootstrap

def daily_net_bps(trades, cost_rt: float):
    """按入场 UTC 天聚合跨币净收益，返回 (有序日 list, 日均值 bps list)。"""
    from collections import defaultdict
    byday = defaultdict(list)
    for tr in trades:
        net = tr["gross"] - cost_rt
        byday[tr["day"]].append(net * 1e4)
    days = sorted(byday)
    return days, [statistics.mean(byday[d]) for d in days]


def moving_block_bootstrap_ci(vals: list[float], block: int, b: int, seed: int):
    """对有序日均值做移动块 block bootstrap，返回 (lo,hi) 95% CI（重采样均值）。"""
    n = len(vals)
    if n < block + 1:
        raise ValueError(f"日数 {n} 少于块长 {block}+1")
    rng = random.Random(seed)
    starts = list(range(0, n - block + 1))
    out = []
    for _ in range(b):
        acc = []
        while len(acc) < n:
            s = starts[rng.randrange(len(starts))]
            acc.extend(vals[s:s + block])
        acc = acc[:n]
        out.append(statistics.mean(acc))
    out.sort()
    return out[int(b * 0.025)], out[int(b * 0.975)]


# ---------------------------------------------------------------- report

def evaluate(trades, cost_rt, block):
    days, bps = daily_net_bps(trades, cost_rt)
    lo, hi = moving_block_bootstrap_ci(bps, block, BOOT_B, BOOT_SEED)
    # 每笔口径
    per_trade = statistics.mean((t["gross"] - cost_rt) * 1e4 for t in trades)
    return {
        "n_trades": len(trades), "n_days": len(days),
        "per_trade_bps": per_trade,
        "day_mean_bps": statistics.mean(bps),
        "ci_lo": lo, "ci_hi": hi,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-trades", action="store_true")
    args = ap.parse_args()

    all_days = list_days("BTCUSDT")
    n = len(all_days)
    split_i = int(n * TRAIN_FRAC)
    split_day = all_days[split_i]
    test_days_set = set(all_days[split_i:])
    print(f"[data] days={n} {all_days[0]}..{all_days[-1]}")
    print(f"[split] train {all_days[0]}..{all_days[split_i-1]} | "
          f"test starts {split_day} ({n-split_i} days)")

    trades = []
    for coin in COINS:
        ct = generate_trades(coin, all_days, split_day)
        # generate_trades 已只保留 >=split；保险再按 day 过滤
        ct = [t for t in ct if t["day"] in test_days_set]
        print(f"[coin] {coin}: test trades={len(ct)}")
        trades.extend(ct)

    print(f"\n=== H1 主口径 COST_RT={COST_MAIN*1e4:.0f}bp block={BLOCK_DAYS}d ===")
    main_res = evaluate(trades, COST_MAIN, BLOCK_DAYS)
    print(f"n_trades={main_res['n_trades']} n_days={main_res['n_days']}")
    print(f"net={main_res['per_trade_bps']:.3f} bps/trade "
          f"day_mean={main_res['day_mean_bps']:.3f} bps")
    print(f"95% CI=[{main_res['ci_lo']:.3f}, {main_res['ci_hi']:.3f}] bps/day")

    print("\n=== 成本敏感性 ===")
    for c in COST_SENS:
        r = evaluate(trades, c, BLOCK_DAYS)
        print(f"cost={c*1e4:.0f}bp net={r['per_trade_bps']:.3f} "
              f"CI=[{r['ci_lo']:.3f},{r['ci_hi']:.3f}]")

    print("\n=== 单币稳健性（主成本）===")
    for coin in COINS:
        sub = [t for t in trades if t["coin"] == coin]
        if sub:
            r = evaluate(sub, COST_MAIN, BLOCK_DAYS)
            print(f"{coin}: n={r['n_trades']} net={r['per_trade_bps']:.3f} "
                  f"CI=[{r['ci_lo']:.3f},{r['ci_hi']:.3f}]")

    print("\n=== 块长稳健性（主成本）===")
    for blk in (1, 10):
        r = evaluate(trades, COST_MAIN, blk)
        print(f"block={blk}d day_mean={r['day_mean_bps']:.3f} "
              f"CI=[{r['ci_lo']:.3f},{r['ci_hi']:.3f}]")

    # 判定
    pass_ci = main_res["ci_lo"] > 0
    pass_econ = main_res["per_trade_bps"] >= 3.0
    pass_n = main_res["n_trades"] >= 300
    print("\n=== H1 判定 ===")
    print(f"CI 下界>0 : {pass_ci}")
    print(f"净均值>=3bps/笔 : {pass_econ}")
    print(f"有效交易>=300 : {pass_n}")
    verdict = pass_ci and pass_econ and pass_n
    print(f"==> H1 {'PASS' if verdict else 'CLOSED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
