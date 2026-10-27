#!/usr/bin/env python3
"""S2 只读：xs_reversal 全历史逐 bar 重放 + 独立 episode/日块统计（离线，无下单）。

目的：诊断显示 xs 超跌反弹的 edge 在 iid 口径 t≈7、但非重叠持仓窗 t≈1.3，
瓶颈是"独立信号日"太少（72h n=28、近35d n=10）。本脚本不依赖稀疏的实时影子
落盘，而是用本地真实 1h K线缓存，在**每一个历史决策 bar** 上重放与生产字节
一致的判定原语（直接 import xs_reversal 的 _ext_pct_series/_awake_frac/
_pctile_rank 与 indicators.ema/rsi），重建全部候选 episode，再：

  1. 计算每个候选（is_candidate = ext 尾部分位 + awake + EMA8<EMA21 +
     RSI[15,35)）的 24/72/168h 前向收益（次根 open 入场，避免前视）；
  2. 报告原始（重叠）样本数与 WR/均值；
  3. **去重叠成独立 episode**：同一币 72h 持仓窗内只保留首个触发，得到非重叠
     交易序列；再按"信号日 UTC"聚合（同日多币共享一个市场，不独立）；
  4. 在独立 episode 与独立信号日两个层级做 t 统计量与按天块 block-bootstrap
     95%CI（不做 iid 假设）；
  5. 同时跑 top_pct=85（生产）与 90（M1 验证），直接暴露配置-验证错配；
  6. 净 edge：默认扣 9.26bp 往返（研究实测 taker），可改 --cost-bps；
  7. 给 S2 go/no-go 判据：72h 独立日 t≥2、bootstrap 下界>0、净均值≥3bp/笔、
     独立信号日数与"距 ~60 独立日"的缺口。

数据：logs/binance_klines_cache/{SYM}_1h.json（list[[t,o,h,l,c,v],...]）。
研究工具，不写配置/不切模式/不下单。

用法：
  python scripts/xs_episode_stats.py BTCUSDT ETHUSDT SOLUSDT
  python scripts/xs_episode_stats.py BTCUSDT --top-pct 85 90 --cost-bps 9.26
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import random
import statistics
from typing import Optional

# 直接复用生产判定原语，保证重放与 evaluate_xs_reversal 同口径。
from hermes_trader.agents.xs_reversal import (
    LOOKBACK_BARS,
    PCTILE_WIN,
    _awake_frac,
    _ext_pct_series,
    _pctile_rank,
)
from hermes_trader.indicators import math as ind
from hermes_trader.models.types import Candle

HOUR_MS = 3_600_000
FWD_HOURS = (24, 72, 168)
HOLD_HOURS = 72          # 去重叠持仓窗（S2 只挂 72h）
RSI_FLOOR, RSI_LONG = 15.0, 35.0
AWAKE_BARS, AWAKE_MIN_FRAC = 7, 0.67
DAY_MS = 24 * HOUR_MS


def load_candles(path: str, coin: str) -> list[Candle]:
    with open(path) as fh:
        raw = json.load(fh)
    out = []
    for b in raw:
        out.append(Candle(t=float(b[0]), o=float(b[1]), h=float(b[2]),
                          l=float(b[3]), c=float(b[4]), v=float(b[5]),
                          coin=coin))
    out.sort(key=lambda c: c.t)
    return out


def replay_coin(candles: list[Candle], top_pct: float,
                cost_bps: float) -> list[dict]:
    """逐 bar 重放，返回候选 episode（含前向毛/净收益）。"""
    n = len(candles)
    closes = [c.c for c in candles]
    highs = [c.h for c in candles]
    vols = [c.v for c in candles]
    ema8 = ind.ema(closes, 8)
    ema21 = ind.ema(closes, 21)
    rsi = ind.rsi(candles, 14)
    ext = _ext_pct_series(closes, highs, LOOKBACK_BARS)
    max_tail = (100.0 - top_pct) / 100.0
    cost = cost_bps / 100.0

    rows = []
    # 滚动维护 ext 的 2160 窗口（已排序），避免每 bar 重排 O(n^2 log)。
    from collections import deque
    window_vals: deque[float] = deque()
    sorted_win: list[float] = []
    first_valid = PCTILE_WIN + LOOKBACK_BARS - 1
    for i in range(n):
        v = ext[i]
        if math.isfinite(v):
            bisect.insort(sorted_win, v)
            window_vals.append(v)
            if len(window_vals) > PCTILE_WIN:
                old = window_vals.popleft()
                del sorted_win[bisect.bisect_left(sorted_win, old)]
        if i < first_valid:
            continue
        if not (math.isfinite(ema8[i]) and math.isfinite(ema21[i])
                and math.isfinite(rsi[i]) and math.isfinite(v)):
            continue
        pctile = _pctile_rank(sorted_win, v)
        if not math.isfinite(pctile) or pctile > max_tail:
            continue
        awake = _awake_frac(candles, vols, i, AWAKE_BARS)
        if not math.isfinite(awake) or awake < AWAKE_MIN_FRAC:
            continue
        downtrend = ema8[i] < ema21[i]
        rsi_ok = RSI_FLOOR <= rsi[i] < RSI_LONG
        is_cand = bool(downtrend and rsi_ok)
        if not is_cand:
            continue
        # 次根 open 入场（可交易、无前视）；前向 hh 用第 i+1+h 根收盘。
        if i + 1 + max(FWD_HOURS) >= n:
            continue
        entry = candles[i + 1].o
        if entry <= 0:
            continue
        rec = {
            "i": i, "ts": int(candles[i].t), "entry": entry,
            "ext_pct": v, "pctile": pctile, "awake": awake,
            "rsi": rsi[i],
        }
        for h in FWD_HOURS:
            px = candles[i + 1 + h].c
            gross = (px / entry - 1.0) * 100.0
            rec[f"fwd{h}_gross"] = gross
            rec[f"fwd{h}_net"] = gross - cost_bps / 100.0
        rows.append(rec)
    return rows


def deoverlap(rows: list[dict], hold_hours: int = HOLD_HOURS) -> list[dict]:
    """同币 72h(=hold_hours 根 1h) 窗内只保留首个触发 → 非重叠 episode。"""
    rows = sorted(rows, key=lambda r: r["i"])
    kept, last_i = [], None
    for r in rows:
        if last_i is None or r["i"] - last_i >= hold_hours:
            kept.append(r)
            last_i = r["i"]
    return kept


def tstat(xs: list[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    sd = statistics.stdev(xs)
    if sd <= 0:
        return float("nan")
    return statistics.fmean(xs) / (sd / math.sqrt(len(xs)))


def day_block_bootstrap(values_by_day: dict[str, list[float]], boot: int,
                        seed: int) -> tuple[float, float]:
    """按信号日重采样（日均值为块），返回均值的 95% 百分位 CI。"""
    day_means = [statistics.fmean(v) for v in values_by_day.values() if v]
    if len(day_means) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    nd = len(day_means)
    sims = []
    for _ in range(boot):
        sims.append(statistics.fmean(
            day_means[rng.randrange(nd)] for _ in range(nd)))
    sims.sort()
    return sims[int(boot * 0.025)], sims[int(boot * 0.975)]


def summarize(label: str, rows: list[dict], field: str, boot: int,
              seed: int) -> dict:
    vals = [r[field] for r in rows if field in r]
    if not vals:
        return {"label": label, "n": 0}
    wins = sum(1 for x in vals if x > 0)
    by_day: dict[str, list[float]] = {}
    import datetime as dt
    for r in rows:
        d = dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(r[field])
    lo, hi = day_block_bootstrap(by_day, boot, seed)
    return {
        "label": label,
        "n_episodes": len(vals),
        "n_signal_days": len(by_day),
        "mean_pct": round(statistics.fmean(vals), 4),
        "median_pct": round(statistics.median(vals), 4),
        "win_rate_pct": round(100.0 * wins / len(vals), 2),
        "t_iid": round(tstat(vals), 3),
        "day_block_ci95": [round(lo, 4), round(hi, 4)]
        if lo == lo else None,
        "mean_bps": round(statistics.fmean(vals) * 100.0, 2),
    }


def analyze_symbol(path: str, top_pcts: tuple[float, ...], cost_bps: float,
                   boot: int, seed: int) -> dict:
    coin = os.path.basename(path).split("_")[0]
    candles = load_candles(path, coin)
    out = {"coin": coin, "bars": len(candles),
           "span": [candles[0].t, candles[-1].t], "variants": {}}
    for tp in top_pcts:
        raw = replay_coin(candles, tp, cost_bps)
        indep = deoverlap(raw)
        v = {
            "n_overlapping_triggers": len(raw),
            "n_independent_episodes": len(indep),
            "n_independent_signal_days": len({
                __import__("datetime").datetime.utcfromtimestamp(
                    r["ts"] / 1000).strftime("%Y-%m-%d") for r in indep}),
            "fwd": {},
        }
        for h in FWD_HOURS:
            tag = str(h)
            v["fwd"][tag] = {
                "overlap_gross": summarize(
                    f"fwd{h} overlap gross", raw, f"fwd{h}_gross", boot, seed),
                "indep_net": summarize(
                    f"fwd{h} indep net", indep, f"fwd{h}_net", boot, seed),
                "indep_gross": summarize(
                    f"fwd{h} indep gross", indep, f"fwd{h}_gross", boot, seed),
            }
        # S2 go/no-go（只看 72h 独立 episode 净）
        s72 = v["fwd"]["72"]["indep_net"]
        if s72.get("n_episodes"):
            ci = s72["day_block_ci95"] or [float("nan"), float("nan")]
            v["s2_gate_72h"] = {
                "indep_t_iid_ge_2": s72["t_iid"] >= 2.0,
                "day_boot_lo_gt_0": ci[0] > 0,
                "net_mean_ge_3bp": s72["mean_bps"] >= 3.0,
                "indep_signal_days": s72["n_signal_days"],
                "gap_to_60_days": max(0, 60 - s72["n_signal_days"]),
            }
        out["variants"][str(tp)] = v
    return out


def analyze_panel(paths: list[str], top_pct: float, cost_bps: float,
                  hold_hours: int, boot: int, seed: int) -> dict:
    """跨币面板：每币先去 72h 重叠，再按 UTC 信号日把同日多币等权成一个
    日块收益（同日多币共享一个市场，不独立）。这是正确的独立推断层级，
    防止在 6 个(币×档位)格子里挑出一个显著的多重检验误判。"""
    import datetime as _dt
    by_day: dict[str, list[float]] = {}
    by_year: dict[str, list[float]] = {}
    n_ep = 0
    for path in paths:
        coin = os.path.basename(path).split("_")[0]
        if not os.path.exists(path):
            continue
        candles = load_candles(path, coin)
        indep = deoverlap(replay_coin(candles, top_pct, cost_bps), hold_hours)
        n_ep += len(indep)
        for r in indep:
            x = r["fwd72_net"]
            d = _dt.datetime.utcfromtimestamp(r["ts"] / 1000)
            by_day.setdefault(d.strftime("%Y-%m-%d"), []).append(x)
            by_year.setdefault(str(d.year), []).append(x)
    day_means = [statistics.fmean(v) for v in by_day.values()]
    lo, hi = day_block_bootstrap(by_day, boot, seed)
    years = {}
    for y, vals in sorted(by_year.items()):
        years[y] = {
            "n_episodes": len(vals),
            "mean_net_pct": round(statistics.fmean(vals), 4),
            "win_rate_pct": round(100.0 * sum(1 for x in vals if x > 0)
                                  / len(vals), 1),
        }
    return {
        "top_pct": top_pct,
        "symbols": len(paths),
        "n_independent_episodes": n_ep,
        "n_independent_signal_days": len(day_means),
        "day_mean_net_pct": round(statistics.fmean(day_means), 4) if day_means else None,
        "day_mean_bps": round(statistics.fmean(day_means) * 100, 2) if day_means else None,
        "day_t_iid": round(tstat(day_means), 3),
        "day_block_ci95": [round(lo, 4), round(hi, 4)] if lo == lo else None,
        "by_year": years,
        "gate_pass": bool(day_means and tstat(day_means) >= 2.0 and lo > 0
                          and statistics.fmean(day_means) * 100 >= 3.0),
    }


def _print_panel(p: dict) -> None:
    print("#" * 80)
    print(f"PANEL (pooled independent signal-days, {p['symbols']} coins, "
          f"top_pct={p['top_pct']})")
    print(f"  independent episodes={p['n_independent_episodes']}  "
          f"independent signal-DAYS={p['n_independent_signal_days']}")
    print(f"  day-equal-weight 72h net: mean={p['day_mean_net_pct']:+}% "
          f"({p['day_mean_bps']:+}bp) t={p['day_t_iid']} dayCI95={p['day_block_ci95']}")
    print("  by calendar year (regime robustness):")
    for y, s in p["by_year"].items():
        print(f"    {y}: n={s['n_episodes']:3} mean={s['mean_net_pct']:+7.3f}% "
              f"WR={s['win_rate_pct']:5.1f}%")
    print(f"  => pooled S2 72h gate (t>=2 & dayCIlo>0 & net>=3bp): "
          f"{'PASS' if p['gate_pass'] else 'NOT-YET'}")
    print("#" * 80)


def _print_report(res: dict, cost_bps: float) -> None:
    import datetime as dt
    s0 = dt.datetime.utcfromtimestamp(res["span"][0] / 1000).date()
    s1 = dt.datetime.utcfromtimestamp(res["span"][1] / 1000).date()
    print("=" * 80)
    print(f"xs_reversal S2 episode stats — {res['coin']}  "
          f"bars={res['bars']} ({s0} -> {s1})  cost={cost_bps}bp rt")
    for tp, v in res["variants"].items():
        print("-" * 80)
        print(f"top_pct={tp}  overlap triggers={v['n_overlapping_triggers']}  "
              f"independent episodes={v['n_independent_episodes']}  "
              f"independent signal-days={v['n_independent_signal_days']}")
        for h in FWD_HOURS:
            cell = v["fwd"][str(h)]
            for key in ("overlap_gross", "indep_gross", "indep_net"):
                s = cell[key]
                if not s.get("n_episodes"):
                    continue
                ci = s["day_block_ci95"]
                print(f"  fwd{h:>3}h {key:14} n={s['n_episodes']:4} "
                      f"days={s['n_signal_days']:4} mean={s['mean_pct']:+7.3f}% "
                      f"({s['mean_bps']:+7.1f}bp) WR={s['win_rate_pct']:5.1f}% "
                      f"t_iid={s['t_iid']:6.2f} dayCI95={ci}")
        g = v.get("s2_gate_72h")
        if g:
            ok = (g["indep_t_iid_ge_2"] and g["day_boot_lo_gt_0"]
                  and g["net_mean_ge_3bp"])
            print(f"  >> 72h S2 gate: t>=2={g['indep_t_iid_ge_2']} "
                  f"dayCIlo>0={g['day_boot_lo_gt_0']} net>=3bp="
                  f"{g['net_mean_ge_3bp']} indep_days={g['indep_signal_days']} "
                  f"gap_to_60={g['gap_to_60_days']}  => "
                  f"{'PASS' if ok else 'NOT-YET'}")
    print("=" * 80)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--top-pct", nargs="+", type=float, default=[85.0, 90.0])
    ap.add_argument("--cost-bps", type=float, default=9.26)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", default=None,
                    help="可选：把 JSON 结果写入文件")
    args = ap.parse_args(argv)

    results = []
    for sym in args.symbols:
        path = os.path.join(args.cache_dir, f"{sym}_1h.json")
        if not os.path.exists(path):
            print(f"[skip] {path} not found")
            continue
        res = analyze_symbol(path, tuple(args.top_pct), args.cost_bps,
                             args.boot, args.seed)
        results.append(res)
        if not args.json:
            _print_report(res, args.cost_bps)

    # 跨币汇总（独立信号日按 (date) 跨币再聚合，避免同日多币重复计独立）
    existing_paths = [os.path.join(args.cache_dir, f"{s}_1h.json")
                      for s in args.symbols]
    existing_paths = [p for p in existing_paths if os.path.exists(p)]
    panels = []
    for tp in args.top_pct:
        p = analyze_panel(existing_paths, tp, args.cost_bps, HOLD_HOURS,
                          args.boot, args.seed)
        panels.append(p)
        if not args.json:
            _print_panel(p)
    payload = {"cost_bps_rt": args.cost_bps, "symbols": results,
               "pooled_panels": panels}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.save:
        with open(args.save, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        if not args.json:
            print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
