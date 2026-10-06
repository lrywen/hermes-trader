#!/usr/bin/env python3
"""Pullback-to-support 入场时序的历史回测（signal-replacement timing test）。

目的
----
假设：在"已确立的上升趋势"里等价格回撤到支撑（EMA21 / 前高回踩），并在
跌势衰竭 + 出现新一根转涨K线时买入，优于在阶段高点追突破。本脚本用同一
OOS 数据集与同一套行情，量化比较两种入场时序的 forward 收益。

数据
----
* 候选/基准：logs/b3_81coin_filt_ra.jsonl（arm=baseline & fired 的真实 OOS
  交易，2026-03-20~09-20，81 币）。基准多单直接使用其 pnl_gross/pnl_net。
* K线：Binance Vision（data-api.binance.vision，支持历史 startTime），1h。
  按 (币, UTC日) gz 缓存到 logs/bt_pullback_cache/。无 USDT 合约的 HL 原生
  币（VVV/FARTCOIN/PURR/MON 等）返回空/400，优雅跳过。

方法（严格无未来函数）
----------------------
1. 对每笔 baseline long fired 候选（时刻 t_ms）：
   - 取 t_ms 时**已收盘**的 1h K线窗口（>=55 根），算 ATR(14)，调用
     pullback_entry()，记录"突破当刻回撤信号是否同时成立"（并发命中率）。
2. 独立扫描（真正的信号替换）：对每个币覆盖期内的 1h 全序列做 walk-forward：
   逐根（按时间顺序）以"截至当根已收盘窗口"调用 pullback_entry()；valid
   即以当根收盘为入场。信号触发后该币冷却 cooldown_bars（默认24）根，
   避免重叠重复计数。
3. 出场模拟只用入场后数据：
   - close-to-close：4/8/24/48 根（4h/8h/24h/48h）固定期限收益（bp）；
   - stop-aware：若入场后任一 1h K线最低价触及 stop_px，记止损价成交
     （最大亏损止损，intrabar low 判定），否则按期限收盘平仓。
   双边手续费 FEE_BP=9.0bp（已核实的 live taker round trip）：
   gross 为毛收益，net = gross - 9.0bp。
4. 对比基准：
   - BASELINE 实际突破多单：n / mean gross bp / mean net bp / 胜率；
   - 重构突破对照（breakout-at-bar）：同序列上"收盘创55根新高且近12根无
     显著回撤"的K线收盘买入，同样冷却、同样 horizon/止损(1.5*ATR) 打分；
   - PULLBACK 各 horizon：n / mean gross / mean net / 胜率。
5. 次级组合（明确标注）：在 pullback 信号上叠加 portfolio vol-target
   （entry_structure.realised_sigma + vol_target_multiplier，目标年化按
   粗年化近似）以及 breakout_regime_veto（对 pullback trigger 实际不生效，
   仅展示接口行为；真正受 veto 影响的是重构突破组，用简易 chop 判定）。

纯标准库 + 本仓指标，与 bt_entry_position.py 风格一致。
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.indicators.math import atr as atr_arr
from hermes_trader.agents.pullback_entry import pullback_entry
from hermes_trader.agents.entry_structure import (
    breakout_regime_veto,
    realised_sigma,
    vol_target_multiplier,
)

CACHE_DIR = _REPO / "logs" / "bt_pullback_cache"

_MS_1H = 3_600_000
_DAY_MS = 86_400_000
FEE_BP = 9.0
HORIZONS = (4, 8, 24, 48)
MIN_BARS = 55
# pullback_entry 默认参数
PB_KW = dict(atr=0.0, trend_ema_p=50, support_ema_p=21, stop_atr_mult=1.5,
             min_pull_pct=0.015, tolerance_atr=0.75)
BREAKOUT_LOOKBACK = 55


# ── K线抓取/缓存（沿用 bt_entry_position 模式，1h = interval "1h"）────────

def _bin_symbol(coin: str) -> str:
    return {"kBONK": "BONK"}.get(coin, coin) + "USDT"


def fetch_day(coin: str, day_start_ms: int) -> list:
    """抓取某 UTC 日（含余量）的 1h K线，磁盘 gz 缓存。返回 [t,o,h,l,c]。

    Binance 返回 -1121/-1100 等错误体（非数组）时，缓存为空标记并返回 []，
    调用方据此跳过该币。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fn = CACHE_DIR / f"{coin}_{day_start_ms}_60.jsonl.gz"
    if fn.exists():
        try:
            with gzip.open(fn, "rt") as f:
                return [json.loads(x) for x in f if x.strip()]
        except Exception:
            fn.unlink(missing_ok=True)
    sym = _bin_symbol(coin)
    qs = (f"symbol={sym}&interval=1h"
          f"&startTime={day_start_ms - _MS_1H}"
          f"&endTime={day_start_ms + _DAY_MS + _MS_1H}&limit=1000")
    url = "https://data-api.binance.vision/api/v3/klines?" + qs
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=12) as r:
                raw = json.loads(r.read())
            if not isinstance(raw, list):
                # 明确的无效品种/参数错误：写空标记，不再重试
                with gzip.open(fn, "wt") as f:
                    f.write("")
                return []
            out = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                    float(x[4])] for x in raw]
            with gzip.open(fn, "wt") as f:
                for row in out:
                    f.write(json.dumps(row) + "\n")
            return out
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    # 网络失败：也写空标记，避免整日重放时反复重连（可用 --skip-prefetch
    # 之外的删除缓存方式强制重试）
    with gzip.open(fn, "wt") as f:
        f.write("")
    return []


def _utc_date(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


# ── 候选/基准加载 ───────────────────────────────────────────────────────────

def load_baseline(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get("type") != "trade" or r.get("arm") != "baseline":
                continue
            if not r.get("fired"):
                continue
            rows.append(r)
    rows.sort(key=lambda r: int(r["entry_t"]))
    return rows


def _day_set(rows: list[dict], days: int) -> set[int]:
    ds = sorted({(int(r["entry_t"]) // _DAY_MS) * _DAY_MS for r in rows})
    if days:
        ds = ds[-days:]
    return set(ds)


# ── 序列工具 ────────────────────────────────────────────────────────────────

def build_series(coin: str, days: set[int]) -> list[list]:
    """拼接该币所需日（含每日前后余量）的 1h K线，去重排序。"""
    seen: dict[int, list] = {}
    for d in sorted(days):
        for row in fetch_day(coin, d):
            seen[row[0]] = row
    return [seen[k] for k in sorted(seen)]


def candles_window(series: list[list], upto_idx: int, n: int) -> list[dict]:
    """series[upto_idx] 为最新已收盘K线，取其及之前最多 n 根为 dict 窗口。"""
    lo = max(0, upto_idx - n + 1)
    return [{"o": r[1], "h": r[2], "l": r[3], "c": r[4]}
            for r in series[lo:upto_idx + 1]]


def forward_returns(series: list[list], entry_idx: int, entry_px: float,
                    stop_px: float) -> dict:
    """只用入场后数据：对每个 horizon 求 close-to-close 与 stop-aware bp。"""
    out = {}
    for h in HORIZONS:
        j = entry_idx + h
        if j >= len(series):
            out[h] = None
            continue
        # close-to-close
        cc = (series[j][4] - entry_px) / entry_px * 1e4
        # stop-aware：入场当根之后每根 low 判定（信号根本身已收盘，不计）
        stopped = False
        for k in range(entry_idx + 1, j + 1):
            if stop_px > 0 and series[k][3] <= stop_px:
                stopped = True
                stop_j = k
                break
        if stopped:
            sa = (stop_px - entry_px) / entry_px * 1e4
        else:
            sa = cc
        out[h] = (cc, sa)
    return out


def _stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0, "sum": 0.0, "mean": float("nan"), "win": float("nan")}
    return {
        "n": len(rets),
        "sum": sum(rets),
        "mean": sum(rets) / len(rets),
        "win": sum(1 for x in rets if x > 0) / len(rets),
    }


def _is_breakout_bar(win: list[dict]) -> bool:
    """重构突破触发：最新收盘创 BREAKOUT_LOOKBACK 根新高，且近12根高点
    基本就是当根高点（无明显回撤可做、追在高点）。"""
    if len(win) < BREAKOUT_LOOKBACK + 1:
        return False
    last = win[-1]["c"]
    prior_hi = max(x["h"] for x in win[-(BREAKOUT_LOOKBACK + 1):-1])
    if last <= prior_hi:
        return False
    recent_hi = max(x["h"] for x in win[-12:])
    # 当根收盘贴在近12根顶部（<0.8% 回撤），即"追高"形态
    return (recent_hi - last) / recent_hi < 0.008


def _simple_regime(win: list[dict]) -> str:
    """极简 regime 近似，仅供 breakout_regime_veto 演示：
    EMA50 向上且收盘在其上 = up；否则若 |收盘-EMA50|/ATR 很小 = chop；
    其余 neutral。"""
    if len(win) < 52:
        return "neutral"
    closes = [x["c"] for x in win]
    k = 2 / 51
    e = closes[0]
    for x in closes:
        e = x * k + e * (1 - k)
    e_prev = closes[0]
    for x in closes[:-3]:
        e_prev = x * k + e_prev * (1 - k)
    a = atr_arr(win, 14)[-1]
    last = closes[-1]
    if e > e_prev and last > e:
        return "up"
    if a > 0 and abs(last - e) / a < 0.75:
        return "chop"
    return "neutral"


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates",
                    default=str(_REPO / "logs/b3_81coin_filt_ra.jsonl"))
    ap.add_argument("--max-coins", type=int, default=0,
                    help="只取候选数最多的前 N 个币（0=全部）")
    ap.add_argument("--coins", default="",
                    help="只跑指定币，逗号分隔，如 ETH 或 ETH,BTC（覆盖 --max-coins）")
    ap.add_argument("--days", type=int, default=0,
                    help="只跑最近 N 个 UTC 日（0=全部可用期）")
    ap.add_argument("--workers", type=int, default=12,
                    help="K线预取并发数")
    ap.add_argument("--skip-prefetch", action="store_true",
                    help="跳过预取（假定本地缓存已齐）")
    ap.add_argument("--cooldown-bars", type=int, default=24,
                    help="同币信号冷却 1h K线根数")
    args = ap.parse_args()

    base = load_baseline(args.candidates)
    day_keep = _day_set(base, args.days)
    base = [r for r in base
            if (int(r["entry_t"]) // _DAY_MS) * _DAY_MS in day_keep]
    longs = [r for r in base if r["side"] == "long"]

    # 选币：按窗口内多单候选数排序
    per_coin: dict[str, int] = {}
    for r in longs:
        per_coin[r["coin"]] = per_coin.get(r["coin"], 0) + 1
    coins = sorted(per_coin, key=lambda c: -per_coin[c])
    if args.coins.strip():
        _want = {x.strip().upper() for x in args.coins.split(",") if x.strip()}
        coins = [c for c in coins if c.upper() in _want]
    elif args.max_coins:
        coins = coins[:args.max_coins]
    coin_set = set(coins)
    longs = [r for r in longs if r["coin"] in coin_set]

    span = sorted(day_keep)
    print(f"窗口 {len(span)} 个 UTC 日："
          f"{_utc_date(span[0])} ~ {_utc_date(span[-1])}；"
          f"币 {len(coins)}；baseline long fired {len(longs)} 笔。")

    # ── 预取：每个币覆盖日（+前后各1日余量）─────────────────────────────
    if not args.skip_prefetch:
        jobs = []
        for c in coins:
            for d in span:
                fn = CACHE_DIR / f"{c}_{d}_60.jsonl.gz"
                if not fn.exists():
                    jobs.append((c, d))
        print(f"需抓取 {len(jobs)} 个 (币,日) 1h K线，{args.workers} 并发…")
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(fetch_day, c, d) for c, d in jobs]
            for fut in futs:
                fut.result()
                done += 1
                if done % 200 == 0:
                    print(f"  {done}/{len(jobs)} ({time.time()-t0:.0f}s)")
        print("K线就绪。")

    # ── 基准：实际突破多单（直接用 JSONL 结果字段）──────────────────────
    b_gross = [float(r["pnl_gross"]) for r in longs]
    b_net = [float(r["pnl_net"]) for r in longs]
    bs_g, bs_n = _stats(b_gross), _stats(b_net)

    # 每币序列
    series_map: dict[str, list[list]] = {}
    skipped = []
    for c in coins:
        s = build_series(c, day_keep)
        if len(s) < MIN_BARS:
            skipped.append(c)
            continue
        series_map[c] = s
    if skipped:
        print(f"跳过无/缺 Binance 1h K线的币 {len(skipped)} 个："
              f"{', '.join(skipped[:12])}"
              f"{' …' if len(skipped) > 12 else ''}")

    # ── 1) 候选时刻并发命中率（突破当根，回撤信号是否同时成立）─────────
    concurrent_hits = 0
    concurrent_checked = 0
    # 建 t->idx 索引（每币）
    idx_map: dict[str, dict[int, int]] = {}
    for c, s in series_map.items():
        idx_map[c] = {row[0]: i for i, row in enumerate(s)}

    for r in longs:
        c = r["coin"]
        if c not in series_map:
            continue
        t = int(r["entry_t"])
        # t_ms 时已收盘：openTime + 1h <= t
        s = series_map[c]
        ui = None
        im = idx_map[c]
        # 候选多为 5m/15m 对齐，找严格收盘的最后一根 1h
        last_t = ((t - _MS_1H) // _MS_1H) * _MS_1H
        ui = im.get(last_t)
        if ui is None:
            # 退化：二分
            lo, hi = 0, len(s) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if s[mid][0] + _MS_1H <= t:
                    lo = mid
                else:
                    hi = mid - 1
            ui = lo if s[lo][0] + _MS_1H <= t else None
        if ui is None or ui < MIN_BARS - 1:
            continue
        win = candles_window(s, ui, 200)
        if len(win) < MIN_BARS:
            continue
        atr_v = atr_arr(win, 14)[-1]
        if not (atr_v > 0):
            continue
        concurrent_checked += 1
        sig = pullback_entry(win, atr=atr_v)
        if sig.valid:
            concurrent_hits += 1

    # ── 2/3) Walk-forward：pullback 与重构突破的独立时序扫描 ───────────
    # 每条记录: entry 收益序列 dict[h]->(cc,sa)，及 vol-target 所需信息
    pb_records: list[dict] = []
    bk_records: list[dict] = []
    bk_veto_records: list[dict] = []

    for c, s in series_map.items():
        last_pb = -10**9
        last_bk = -10**9
        for i in range(MIN_BARS - 1, len(s) - 1):
            win = candles_window(s, i, 200)
            atr_v = atr_arr(win, 14)[-1]
            if not (atr_v > 0):
                continue

            sig = pullback_entry(win, atr=atr_v)
            if sig.valid and i - last_pb >= args.cooldown_bars:
                last_pb = i
                fr = forward_returns(s, i, sig.entry_px, sig.stop_px)
                if any(v is not None for v in fr.values()):
                    pb_records.append({"coin": c, "idx": i, "fr": fr,
                                       "atr_pct": atr_v / sig.entry_px})

            if _is_breakout_bar(win) and i - last_bk >= args.cooldown_bars:
                last_bk = i
                stop = min(win[-1]["c"] - 1.5 * atr_v,
                           win[-1]["c"] - 1.0 * atr_v)
                fr = forward_returns(s, i, win[-1]["c"], stop)
                if any(v is not None for v in fr.values()):
                    rec = {"coin": c, "idx": i, "fr": fr,
                           "atr_pct": atr_v / win[-1]["c"]}
                    bk_records.append(rec)
                    regime = _simple_regime(win)
                    if breakout_regime_veto(regime=regime,
                                            trigger="breakout") is None:
                        bk_veto_records.append(rec)

    # ── 4) 次级组合：pullback + portfolio vol-target ───────────────────
    # 用各信号当根之前 24 根的 1h 对数收益算 sigma，sqrt(24*365) 粗年化；
    # 目标年化 60%。mult 作用于"存在该仓位"的收益贡献（bp*mult），
    # 仅示意组合层缩放方向。
    TARGET_SIGMA_ANNUAL = 0.60
    pb_vt_records: list[dict] = {}
    for rec in pb_records:
        s = series_map[rec["coin"]]
        i = rec["idx"]
        rets = []
        for k in range(max(1, i - 23), i + 1):
            p0, p1 = s[k - 1][4], s[k][4]
            if p0 > 0:
                rets.append(math.log(p1 / p0))
        sig = realised_sigma(rets)
        mult = 1.0
        if sig is not None:
            mult = vol_target_multiplier(
                sigma=sig * math.sqrt(24 * 365),
                target_sigma=TARGET_SIGMA_ANNUAL)
        pb_vt_records[rec["coin"], rec["idx"]] = mult

    # pullback 触发本身不被 breakout veto（接口行为验证）
    _veto_on_pullback = breakout_regime_veto(regime="chop", trigger="pullback")

    # ── 汇总打印 ─────────────────────────────────────────────────────────
    def collect(records: list[dict], mode: str, scaled: bool = False):
        res = {}
        for h in HORIZONS:
            gross, net = [], []
            for rec in records:
                v = rec["fr"].get(h)
                if v is None:
                    continue
                r = v[0] if mode == "close" else v[1]
                if scaled:
                    m = pb_vt_records.get((rec["coin"], rec["idx"]), 1.0)
                    r = r * m
                gross.append(r)
                net.append(r - FEE_BP)
            res[h] = (_stats(gross), _stats(net))
        return res

    pb_close = collect(pb_records, "close")
    pb_stop = collect(pb_records, "stop")
    pb_stop_vt = collect(pb_records, "stop", scaled=True)
    bk_close = collect(bk_records, "close")
    bk_stop = collect(bk_records, "stop")
    bkv_stop = collect(bk_veto_records, "stop")

    print("\n" + "=" * 78)
    print("BASELINE — 实际突破多单（JSONL，系统真实出场口径）")
    print(f"  n={bs_n['n']:6}  meanGross={bs_g['mean']:7.2f}bp  "
          f"meanNet={bs_n['mean']:7.2f}bp  win={bs_n['win']:.3f}  "
          f"sumNet={bs_n['sum']:.0f}bp")

    print("\nPULLBACK 信号在突破候选当根的并发命中率："
          f"{concurrent_hits}/{concurrent_checked} "
          f"({(concurrent_hits/concurrent_checked*100 if concurrent_checked else 0):.2f}%)"
          "  ← 两种入场几乎不在同一根K线，故以下以独立 walk-forward 时序比较")

    def print_block(title: str, res: dict, extra: str = ""):
        print(f"\n{title}{extra}")
        print(f"  {'horizon':8} {'n':>6} {'meanGross':>10} {'meanNet':>9} "
              f"{'winNet':>7} {'sumNet':>9}")
        for h in HORIZONS:
            sg, sn = res[h]
            if sn["n"] == 0:
                print(f"  {h}h{'':<5} {'0':>6}")
                continue
            print(f"  {h}h{'':<5} {sn['n']:>6} {sg['mean']:>10.2f} "
                  f"{sn['mean']:>9.2f} {sn['win']:>7.3f} {sn['sum']:>9.0f}")

    print_block("PULLBACK — close-to-close（手续费 %g bp/round）" % FEE_BP,
                pb_close)
    print_block("PULLBACK — stop-aware（触及 stop_px 记止损）", pb_stop)
    print_block("PULLBACK+VOLTARGET — stop-aware（次级组合，目标年化60%）",
                pb_stop_vt)
    print_block("重构 BREAKOUT — close-to-close（同口径对照）", bk_close)
    print_block("重构 BREAKOUT — stop-aware", bk_stop)
    print_block("重构 BREAKOUT + regime veto — stop-aware（次级组合）",
                bkv_stop)

    print("\n" + "-" * 78)
    print(f"pullback 信号总数（冷却后，去重）：{len(pb_records)}；"
          f"重构突破信号总数：{len(bk_records)}；"
          f"veto 后突破：{len(bk_veto_records)}")
    print(f"breakout_regime_veto 对 trigger='pullback' 返回："
          f"{_veto_on_pullback!r}（None=回撤入场不受突破否决影响，符合设计）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
