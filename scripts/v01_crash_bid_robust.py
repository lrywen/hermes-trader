#!/usr/bin/env python3
"""V-01：crash 左侧承接因子（LEFT 24h +13.48bp stop-aware）的独立稳健性复核。

来源线索：logs/bt_confirm_full.log（bt_counter_trend_confirm.py 产物）显示
crash 当根收盘买入在 24h stop-aware 净 +13.48bp（n=2616），而"等确认"每个
horizon 都更差。本脚本不重复"等确认"，只压力测试这个**正期望候选**是否站得住。

四个能让正收益假性成立的偏差，逐一切换（默认口径=原始日志，便于对照）：

1. 可执行价（--exec next_open）：原始口径在 crash 当根**收盘**同时成交，实盘
   无法在收盘瞬间以收盘价成交；改为**下一根 open** 入场（次根 open 可挂单）。
2. 入场当根止损（--stop-include-entry）：原始止损只看 entry 之后的 low；若
   在收盘价进场，当根剩余时间（次根才真正开始）——次根 open 口径下从次根起算。
3. 费用（--fee-bp）：默认 9bp，另测更保守口径。
4. 参数/集中度：--min-drop/--atr-mult/--cooldown 敏感性 + 分币贡献与 leave-one-out。

信号定义与原始脚本一致（纯价）：EMA50 下行且收盘在其下（regime=down）；
当根阴线 body≥min_drop_pct 或 ≥atr_mult×ATR(14)；冷却去重。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from hermes_trader.indicators.math import atr as atr_arr  # noqa: E402
import bt_pullback_entry as btpb  # noqa: E402

HORIZONS = (4, 8, 24, 48)
MIN_BARS = 55
DAY_MS = 86_400_000


def forward(series, ei, px, stop, *, stop_include_entry):
    """做多 forward（入场 idx=ei，价 px，止损 stop）。返回每 horizon(cc,sa)。"""
    out = {}
    start = ei if stop_include_entry else ei + 1
    for h in HORIZONS:
        j = ei + h
        if j >= len(series):
            out[h] = None
            continue
        cc = (series[j][4] - px) / px * 1e4
        sj = None
        for k in range(start, j + 1):
            if stop > 0 and series[k][3] <= stop:
                sj = k
                break
        sa = (stop - px) / px * 1e4 if sj else cc
        out[h] = (cc, sa)
    return out


def scan(series_map, *, exec_next_open, stop_include_entry,
         min_drop_pct, atr_mult, cooldown):
    """返回 records[{coin,idx,fr}] 与 crash 事件数。"""
    records: list[dict] = []
    n_events = 0
    for c, s in series_map.items():
        n = len(s)
        closes = [r[4] for r in s]
        opens = [r[1] for r in s]
        allw = [{"o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in s]
        atr_full = atr_arr(allw, 14)
        k50 = 2 / 51
        ema50 = [closes[0]] * n
        for xi in range(1, n):
            ema50[xi] = closes[xi] * k50 + ema50[xi - 1] * (1 - k50)

        last_event = -10**9
        for i in range(MIN_BARS - 1, n - 1):
            atr_v = atr_full[i]
            if not (atr_v == atr_v and atr_v > 0):
                continue
            if not (ema50[i] < ema50[i - 3] and closes[i] < ema50[i]):
                continue
            o, cl = opens[i], closes[i]
            is_crash = (o > cl and o > 0 and
                        ((o - cl) / o >= min_drop_pct or
                         (o - cl) >= atr_mult * atr_v))
            if not is_crash:
                continue
            if i - last_event < cooldown:
                continue
            last_event = i
            n_events += 1

            ei = i
            px = cl
            if exec_next_open:
                ei = i + 1
                if ei >= n:
                    continue
                px = opens[ei]
            stop = px - atr_mult * atr_full[ei if ei < n else i]
            # 原始口径止损 1.5×ATR；保持 1.5 与日志一致（at_mult 仅用于crash判定）
            stop = px - 1.5 * (atr_full[ei] if ei < n else atr_v)
            fr = forward(s, ei, px, stop,
                         stop_include_entry=stop_include_entry)
            if any(v is not None for v in fr.values()):
                records.append({"coin": c, "idx": ei, "fr": fr})
    return records, n_events


def summarize(records, fee_bp):
    res = {}
    for h in HORIZONS:
        gross, net = [], []
        for rec in records:
            v = rec["fr"].get(h)
            if v is None:
                continue
            gross.append(v[1])          # stop-aware
            net.append(v[1] - fee_bp)
        res[h] = (btpb._stats(gross), btpb._stats(net))
    return res


def per_coin_24h(records, fee_bp):
    d: dict[str, list[float]] = {}
    for rec in records:
        v = rec["fr"].get(24)
        if v is None:
            continue
        d.setdefault(rec["coin"], []).append(v[1] - fee_bp)
    out = [(c, len(x), sum(x)) for c, x in d.items()]
    out.sort(key=lambda t: -t[2])
    return out


def print_block(tag, res):
    print(f"\n{tag}")
    print(f"  {'horizon':8}{'n':>6}{'meanGross':>11}{'meanNet':>10}"
          f"{'winNet':>8}{'sumNet':>10}")
    for h in HORIZONS:
        sg, sn = res[h]
        if sn["n"] == 0:
            print(f"  {h}h{'':<5}{'0':>6}")
            continue
        print(f"  {h}h{'':<5}{sn['n']:>6}{sg['mean']:>11.2f}"
              f"{sn['mean']:>10.2f}{sn['win']:>8.3f}{sn['sum']:>10.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-coins", type=int, default=0)
    ap.add_argument("--coins", default="")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--exec", choices=["close", "next_open"], default="close")
    ap.add_argument("--stop-include-entry", action="store_true")
    ap.add_argument("--fee-bp", type=float, default=9.0)
    ap.add_argument("--min-drop", type=float, default=0.015)
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--cooldown", type=int, default=24)
    args = ap.parse_args()

    if args.offline:
        import gzip as _gz

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
    per_coin: dict[str, int] = {}
    for r in base:
        per_coin[r["coin"]] = per_coin.get(r["coin"], 0) + 1
    coins = sorted(per_coin, key=lambda c: -per_coin[c])
    if args.coins.strip():
        want = {x.strip().upper() for x in args.coins.split(",") if x.strip()}
        coins = [c for c in coins if c.upper() in want]
    elif args.max_coins:
        coins = coins[:args.max_coins]

    span = sorted(day_keep)
    print(f"窗口 {len(span)} UTC 日 {btpb._utc_date(span[0])}~"
          f"{btpb._utc_date(span[-1])}；币 {len(coins)}")

    series_map = {}
    skipped = []
    for c in coins:
        s = btpb.build_series(c, day_keep)
        if len(s) < MIN_BARS:
            skipped.append(c)
            continue
        series_map[c] = s
    if skipped:
        print(f"跳过缺K线 {len(skipped)}：{', '.join(skipped[:12])}")

    records, n_events = scan(
        series_map,
        exec_next_open=(args.exec == "next_open"),
        stop_include_entry=args.stop_include_entry,
        min_drop_pct=args.min_drop, atr_mult=args.atr_mult,
        cooldown=args.cooldown)

    res = summarize(records, args.fee_bp)
    print("\n" + "=" * 80)
    print(f"crash 事件 {n_events}；记录 {len(records)} | exec={args.exec} "
          f"stop_incl_entry={args.stop_include_entry} fee={args.fee_bp} "
          f"min_drop={args.min_drop} atr_mult={args.atr_mult} cd={args.cooldown}")
    print_block("crash 左侧承接（stop-aware）", res)

    pc24 = per_coin_24h(records, args.fee_bp)
    print("\n24h 净 sum 贡献 top/bottom 10 币：")
    print(f"  {'coin':10}{'n':>6}{'sumNet24':>11}")
    for c, cnt, tot in pc24[:10]:
        print(f"  {c:10}{cnt:>6}{tot:>11.0f}")
    print("  ...")
    for c, cnt, tot in pc24[-10:]:
        print(f"  {c:10}{cnt:>6}{tot:>11.0f}")
    pos = [t for t in pc24 if t[2] > 0]
    print(f"正贡献币 {len(pos)}/{len(pc24)}；总24h净 {sum(t[2] for t in pc24):.0f}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
