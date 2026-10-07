#!/usr/bin/env python3
"""逆势抄底"止跌确认"闸门的配对历史回测。

假设
----
Audit 2026-10-07（AVAX）：在 regime=down（逆势）中于一根急跌 K 线（crash
bar）当根收盘直接做多 = 左侧接飞刀，常 MFE 0 后止损。改为**等待跌势后第
一根 fresh up-bar（止跌确认）**再入场，应改善 forward 收益。本脚本用同一
序列、同一批 crash 事件做严格配对比较：

* LEFT    — crash bar 当根收盘买入（旧行为，无确认）。
* CONFIRM — crash 后 max_wait 根内出现第一根 fresh up-bar 的收盘买入；期
             限内无确认则**放弃**（即新闸门"拦截"该笔）。

两种口径打分：close-to-close（4/8/24/48h）与 stop-aware（1.5×ATR 止损，
intrabar low 判定），往返费 FEE_BP=9bp。信号仅用截至当根已收盘信息；
forward 数据只用于打分。

数据
----
K线复用 bt_pullback_entry 的 Binance Vision gz 缓存
（logs/bt_pullback_cache/）；如缓存缺失可用本脚本 --prefetch 触发抓取
（复用 bt_pullback_entry.fetch_day）。regime 用与生产 market_regime 同一
口径的 EMA50 近似（EMA50 下行且收盘在其下 = down）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.indicators.math import atr as atr_arr
from hermes_trader.agents.pullback_entry import crash_bar, fresh_up_bar

HORIZONS = (4, 8, 24, 48)
FEE_BP = 9.0
MIN_BARS = 55
MS_1H = 3_600_000
DAY_MS = 86_400_000


# ── 复用 pullback 回测的抓取/序列工具 ────────────────────────────────────────
sys.path.insert(0, str(_REPO / "scripts"))
import bt_pullback_entry as btpb  # noqa: E402


def candles_window(series, upto_idx, n):
    return btpb.candles_window(series, upto_idx, n)


def forward_returns(series, entry_idx, entry_px, stop_px):
    return btpb.forward_returns(series, entry_idx, entry_px, stop_px)


def _stats(rets):
    return btpb._stats(rets)


def regime_down(win) -> bool:
    """与生产口径一致的逆势判定（近似）：EMA50 下行且最新收盘在其下。"""
    if len(win) < 55:
        return False
    closes = [x["c"] for x in win]
    k = 2 / 51
    e = closes[0]
    for x in closes:
        e = x * k + e * (1 - k)
    e3 = closes[0]
    for x in closes[:-3]:
        e3 = x * k + e3 * (1 - k)
    return bool(e < e3 and closes[-1] < e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-coins", type=int, default=0)
    ap.add_argument("--coins", default="", help="逗号分隔，如 ETH 或 AVAX,ETH")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--prefetch", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="只用本地缓存，缺日不触发网络抓取（避免挂起）")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--max-wait", type=int, default=6,
                    help="crash 后等待 fresh up-bar 的最多 1h 根数")
    ap.add_argument("--min-drop-pct", type=float, default=0.015)
    ap.add_argument("--cooldown-bars", type=int, default=24)
    args = ap.parse_args()

    # --offline：把 fetch_day 替换为缓存只读（缺日返回 []，绝不联网）
    if args.offline:
        import gzip as _gz

        _orig_fetch = btpb.fetch_day

        def _cache_only(coin, day_start_ms):
            fn = btpb.CACHE_DIR / f"{coin}_{day_start_ms}_60.jsonl.gz"
            if fn.exists():
                try:
                    with _gz.open(fn, "rt") as f:
                        return [json.loads(x) for x in f if x.strip()]
                except Exception:
                    return []
            return []

        btpb.fetch_day = _cache_only

    base = btpb.load_baseline(str(_REPO / "logs/b3_81coin_filt_ra.jsonl"))
    day_keep = btpb._day_set(base, args.days)
    base = [r for r in base
            if (int(r["entry_t"]) // DAY_MS) * DAY_MS in day_keep]
    longs = [r for r in base if r["side"] == "long"]

    per_coin = {}
    for r in longs:
        per_coin[r["coin"]] = per_coin.get(r["coin"], 0) + 1
    coins = sorted(per_coin, key=lambda c: -per_coin[c])
    if args.coins.strip():
        want = {x.strip().upper() for x in args.coins.split(",") if x.strip()}
        coins = [c for c in coins if c.upper() in want]
    elif args.max_coins:
        coins = coins[:args.max_coins]

    span = sorted(day_keep)
    print(f"窗口 {len(span)} 个 UTC 日：{btpb._utc_date(span[0])} ~ "
          f"{btpb._utc_date(span[-1])}；币 {len(coins)}。")

    if args.prefetch:
        from concurrent.futures import ThreadPoolExecutor
        import time
        jobs = [(c, d) for c in coins for d in span
                if not (btpb.CACHE_DIR / f"{c}_{d}_60.jsonl.gz").exists()]
        print(f"抓取 {len(jobs)} 个(币,日)…")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(btpb.fetch_day, c, d) for c, d in jobs]
            for i, f in enumerate(futs):
                f.result()
                if (i + 1) % 200 == 0:
                    print(f"  {i+1}/{len(jobs)}")
        print("K线就绪。")

    series_map = {}
    skipped = []
    for c in coins:
        s = btpb.build_series(c, day_keep)
        if len(s) < MIN_BARS:
            skipped.append(c)
            continue
        series_map[c] = s
    if skipped:
        print(f"跳过缺K线币 {len(skipped)} 个：{', '.join(skipped[:12])}")

    # ── 配对扫描 ─────────────────────────────────────────────────────────
    left_records: list[dict] = []
    conf_records: list[dict] = []
    crash_events = 0

    for c, s in series_map.items():
        n = len(s)
        closes = [r[4] for r in s]
        opens = [r[1] for r in s]
        # 一次性预算 ATR(14) 全序列（dict 蜡烛供指标消费）
        allw = [{"o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in s]
        atr_full = atr_arr(allw, 14)
        # 一次性预算 EMA50（及 -3 位）用于 regime=down
        k50 = 2 / 51
        ema50 = [closes[0]] * n
        for x_i in range(1, n):
            ema50[x_i] = closes[x_i] * k50 + ema50[x_i - 1] * (1 - k50)
        # 当根是否阴线收低于前收（fresh up 的否定形态用 closes/oopens 直判）
        last_event = -10**9
        for i in range(MIN_BARS - 1, n - 1):
            atr_v = atr_full[i]
            if not (atr_v == atr_v and atr_v > 0):
                continue
            # regime=down：EMA50 下行（对比 -3）且收盘在其下
            if not (ema50[i] < ema50[i - 3] and closes[i] < ema50[i]):
                continue
            o, cl = opens[i], closes[i]
            # crash 当根：阴线 body 达 min_drop_pct 或 1.5×ATR
            is_crash = (o > cl and o > 0 and
                        ((o - cl) / o >= args.min_drop_pct or
                         (o - cl) >= 1.5 * atr_v))
            if not is_crash:
                continue
            if i - last_event < args.cooldown_bars:
                continue
            last_event = i
            crash_events += 1

            # LEFT：crash 当根收盘
            left_px = cl
            left_stop = min(left_px - 1.5 * atr_v, left_px - 1.0 * atr_v)
            fr_l = forward_returns(s, i, left_px, left_stop)
            if any(v is not None for v in fr_l.values()):
                left_records.append({"coin": c, "idx": i, "fr": fr_l})

            # CONFIRM：后续 max_wait 根内第一根 fresh up-bar
            ci = None
            for j in range(i + 1, min(n - 1, i + args.max_wait) + 1):
                if closes[j] > opens[j] and closes[j] > closes[j - 1]:
                    ci = j
                    break
            if ci is None:
                continue  # 无确认 → 新闸门放弃该笔
            conf_px = closes[ci]
            atr_c = atr_full[ci]
            if not (atr_c == atr_c and atr_c > 0):
                continue
            conf_stop = min(conf_px - 1.5 * atr_c, conf_px - 1.0 * atr_c)
            fr_c = forward_returns(s, ci, conf_px, conf_stop)
            if any(v is not None for v in fr_c.values()):
                conf_records.append({"coin": c, "idx": ci, "fr": fr_c})

    # ── 汇总 ─────────────────────────────────────────────────────────────
    def collect(records, mode):
        res = {}
        for h in HORIZONS:
            gross, net = [], []
            for rec in records:
                v = rec["fr"].get(h)
                if v is None:
                    continue
                r = v[0] if mode == "close" else v[1]
                gross.append(r)
                net.append(r - FEE_BP)
            res[h] = (_stats(gross), _stats(net))
        return res

    l_close = collect(left_records, "close")
    l_stop = collect(left_records, "stop")
    c_close = collect(conf_records, "close")
    c_stop = collect(conf_records, "stop")

    def block(title, res):
        print(f"\n{title}")
        print(f"  {'horizon':8}{'n':>6}{'meanGross':>11}{'meanNet':>10}"
              f"{'winNet':>8}{'sumNet':>10}")
        for h in HORIZONS:
            sg, sn = res[h]
            if sn["n"] == 0:
                print(f"  {h}h{'':<5}{'0':>6}")
                continue
            print(f"  {h}h{'':<5}{sn['n']:>6}{sg['mean']:>11.2f}"
                  f"{sn['mean']:>10.2f}{sn['win']:>8.3f}{sn['sum']:>10.0f}")

    print("\n" + "=" * 80)
    print(f"逆势 crash 事件（冷却后去重）：{crash_events}；"
          f"LEFT 入场 {len(left_records)}；CONFIRM 入场 {len(conf_records)}")
    print(f"确认率（crash 后 {args.max_wait}h 内出现 fresh up-bar）："
          f"{(len(conf_records)/crash_events*100 if crash_events else 0):.1f}%")
    block("LEFT（crash 当根收盘买入）— close-to-close", l_close)
    block("LEFT — stop-aware（1.5×ATR 止损）", l_stop)
    block("CONFIRM（等 fresh up-bar）— close-to-close", c_close)
    block("CONFIRM — stop-aware", c_stop)

    print("\n" + "-" * 80)
    print("逐 horizon 净差（CONFIRM stop-aware − LEFT stop-aware，bp）：")
    for h in HORIZONS:
        a = c_stop[h][1]["mean"]
        b = l_stop[h][1]["mean"]
        if a == a and b == b:
            print(f"  {h}h: {a - b:+.2f} bp")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
