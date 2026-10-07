#!/usr/bin/env python3
"""弱趋势下跌透后"禁止追空"闸门的配对历史回测。

假设
----
Audit 2026-10-07（ENA，shadow）：价格 24h 已 −7.9%、ADX4h 仅 21（弱趋势）、
入场贴近成交密集支撑（非新破位），仍按"下跌动量+放量"做空 = 跌透后追空，
84 分钟内反弹触损 −12% ROE。假设在**弱趋势(ADX 低)且近窗已大幅下跌(延伸)**
时放弃做空，能避开这类负期望追空。用同一序列、同一批做空信号做严格配对：

* BASELINE — 信号当根收盘做空（旧行为）。
* VETO     — 同信号，但若 ADX(14) < weak_adx 且近 24 根跌幅 ≥ ext_drop
             （即弱趋势 + 已大幅延伸）则**放弃**；其余照常做空。

简化信号（本地 1h K线缓存仅 OHLC、无成交量，无法复刻放量）：EMA50 下行且
最新收盘在其下（regime=down，与生产 market_regime 同口径）且近 24 根跌幅
≤ −min_drop（下跌动量）。因此信号是生产做空的"价格超集"，结论为方向性参考。

打分：做空 close-to-close（4/8/24/48h）与 stop-aware（上方 1.5×ATR 止损，
intrabar high 判定）；往返 FEE_BP=9bp。信号仅用截至当根已收盘信息；forward
数据只用于打分。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from hermes_trader.indicators.math import adx as adx_arr  # noqa: E402
from hermes_trader.indicators.math import atr as atr_arr  # noqa: E402
import bt_pullback_entry as btpb  # noqa: E402

HORIZONS = (4, 8, 24, 48)
FEE_BP = 9.0
MIN_BARS = 55
LOOKBACK = 24          # 下跌动量 / 延伸 的回看根数（24h）
DAY_MS = 86_400_000


def short_forward(series, entry_idx, entry_px, stop_px):
    """做空 forward：每 horizon 求 close-to-close 与 stop-aware bp（上方止损，
    用入场当根之后的 high 判定）。做空收益 = 入场价相对出场价的跌幅。"""
    out = {}
    for h in HORIZONS:
        j = entry_idx + h
        if j >= len(series):
            out[h] = None
            continue
        cc = (entry_px - series[j][4]) / entry_px * 1e4
        stop_j = None
        for k in range(entry_idx + 1, j + 1):
            if stop_px > 0 and series[k][2] >= stop_px:
                stop_j = k
                break
        sa = (entry_px - stop_px) / entry_px * 1e4 if stop_j else cc
        out[h] = (cc, sa)
    return out


def _stats(rets):
    return btpb._stats(rets)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-coins", type=int, default=0)
    ap.add_argument("--coins", default="", help="逗号分隔，如 ENA 或 ENA,ETH")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--offline", action="store_true",
                    help="只用本地缓存，缺日不联网")
    ap.add_argument("--min-drop", type=float, default=0.03,
                    help="做空信号：近24根最小跌幅（3%%）")
    ap.add_argument("--weak-adx", type=float, default=30.0,
                    help="veto：ADX 低于此视为弱趋势")
    ap.add_argument("--ext-drop", type=float, default=0.07,
                    help="veto：近24根跌幅达到此视为已大幅延伸（7%%）")
    ap.add_argument("--cooldown-bars", type=int, default=24)
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
    print(f"窗口 {len(span)} 个 UTC 日：{btpb._utc_date(span[0])} ~ "
          f"{btpb._utc_date(span[-1])}；币 {len(coins)}。")

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

    base_records: list[dict] = []
    kept_records: list[dict] = []
    n_signals = n_vetoed = 0

    for c, s in series_map.items():
        n = len(s)
        closes = [r[4] for r in s]
        allw = [{"o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in s]
        atr_full = atr_arr(allw, 14)
        adx_full = adx_arr(allw, 14)
        # EMA50 全序列（regime=down：下行且收盘在其下）
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
            # 下跌动量：近 24 根跌幅 ≤ −min_drop
            j0 = max(0, i - LOOKBACK)
            drop = (closes[j0] - closes[i]) / closes[j0]
            if drop < args.min_drop:
                continue
            if i - last_event < args.cooldown_bars:
                continue
            last_event = i
            n_signals += 1

            px = closes[i]
            stop = px + 1.5 * atr_v
            fr = short_forward(s, i, px, stop)
            if any(v is not None for v in fr.values()):
                base_records.append({"coin": c, "idx": i, "fr": fr})

            # veto：弱趋势 ADX 且已大幅延伸 → 放弃
            adx_v = adx_full[i]
            weak = (adx_v == adx_v) and adx_v < args.weak_adx
            extended = drop >= args.ext_drop
            if weak and extended:
                n_vetoed += 1
                continue
            if any(v is not None for v in fr.values()):
                kept_records.append({"coin": c, "idx": i, "fr": fr})

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

    b_close = collect(base_records, "close")
    b_stop = collect(base_records, "stop")
    k_close = collect(kept_records, "close")
    k_stop = collect(kept_records, "stop")

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
    print(f"做空信号（冷却去重）：{n_signals}；被 veto 拦截：{n_vetoed} "
          f"({n_vetoed / max(1, n_signals) * 100:.1f}%)")
    print(f"参数：min_drop={args.min_drop:.0%} weak_adx<{args.weak_adx:.0f} "
          f"ext_drop≥{args.ext_drop:.0%}")
    block("BASELINE（信号即做空）— close-to-close", b_close)
    block("BASELINE — stop-aware（1.5×ATR 止损）", b_stop)
    block("VETO（剔除弱趋势+延伸）— close-to-close", k_close)
    block("VETO — stop-aware", k_stop)

    print("\n" + "-" * 80)
    print("逐 horizon 净差（VETO stop-aware − BASELINE stop-aware，bp）：")
    for h in HORIZONS:
        a = k_stop[h][1]["mean"]
        b = b_stop[h][1]["mean"]
        if a == a and b == b:
            print(f"  {h}h: {a - b:+.2f} bp")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
