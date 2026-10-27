#!/usr/bin/env python3
"""S2 每日日块统计任务（离线/只读；时间记录优先历史数据）。

合并改造方案 S2 的两项要求：
  * 3.2.2 每日输出：独立 episode 数、72h 净收益、日块均值 t、留一剔月最差、
    币种为正比例（广度）、前 5% 信号集中度；
  * 3.2.1 / P1b：top_pct 85（生产）vs 90（M1 验证）影子双计数，决定生产应跑
    哪一格。

数据源默认是本地真实 1h K线（历史重放，确定性、样本足）；也可 --jsonl 指向
xs_reversal_shadow.jsonl 对实时影子落盘做同样聚合（jsonl 行若尚未回填 outcome
则前向收益为空，会单独计数）。波动腿同时给 none/fixed(atr>=1.0)/pctile(>=0.6)
三列，对照 record-only 门的增益。所有口径与 xs_episode_stats /
xs_vol_gate_calibration 一致（72h 去重叠、按 UTC 信号日日块）。

用法：
  python scripts/xs_s2_daily_dashboard.py                 # 历史K线，85与90
  python scripts/xs_s2_daily_dashboard.py --jsonl /data/xs_reversal_shadow.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
from typing import Optional

from scripts.xs_episode_stats import (
    day_block_bootstrap,
    tstat,
)
from scripts.xs_vol_gate_calibration import episodes_with_atr_pctile

EXT_MAX = -8.0
RSI_MIN = 20.0
VOL_LEGS = {
    "none": lambda r: True,
    "fixed1.0": lambda r: r["atr24_pct"] >= 1.0,
    "pctile0.6": lambda r: r["atr_pctile"] >= 0.6,
}


def leave_one_month_out_worst(rows: list[dict]) -> Optional[float]:
    """留一剔月：每次剔除一个自然月后其余 72h 净均值（bp），返回最差值。
    要求 >0（方案 S2 go 判据）。"""
    def month(r):
        return dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m")
    months = sorted({month(r) for r in rows})
    if len(months) < 2:
        return None
    worst = None
    for drop in months:
        rest = [r["fwd72_net"] for r in rows if month(r) != drop]
        if rest:
            m = statistics.fmean(rest) * 100.0
            worst = m if worst is None else min(worst, m)
    return round(worst, 1) if worst is not None else None


def coin_breadth(rows: list[dict]) -> tuple[float, int]:
    """币种广度：有信号且均值为正的币占比，及总币数。"""
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r.get("coin", "?"), []).append(r["fwd72_net"])
    if not by:
        return float("nan"), 0
    pos = sum(1 for v in by.values() if statistics.fmean(v) > 0)
    return round(100.0 * pos / len(by), 1), len(by)


def top5pct_concentration(rows: list[dict]) -> Optional[float]:
    """前 5% 信号（按 72h 净）贡献的总利润占比（越高越依赖少数赢家）。"""
    vals = sorted((r["fwd72_net"] for r in rows), reverse=True)
    if not vals:
        return None
    total = sum(vals)
    k = max(1, math.ceil(0.05 * len(vals)))
    top = sum(v for v in vals[:k] if v > 0)
    if total <= 0:
        return None
    return round(100.0 * top / total, 1)


def cluster_panic_events(day_vals: dict[str, float], gap_days: int) -> list[float]:
    """把相邻（间隔<=gap_days）活跃的 UTC 信号日合并为 1 个独立恐慌事件。

    72h 持仓窗内、同一波市场级砸盘会在多币/相邻日重复触发；按笔或按"日"都会
    把它们误当独立样本（HL 全 81 币复核：80 个信号日去相关后只剩 3 个独立事件）。
    事件收益取窗内各活跃日日收益的等权平均（保守，不重复计 72h 重叠）。
    """
    days = sorted(day_vals)
    events: list[float] = []
    cur: list[float] = []
    last = None
    for d in days:
        dd = dt.date.fromisoformat(d)
        if last is not None and (dd - last).days > gap_days:
            events.append(statistics.fmean(cur))
            cur = []
        cur.append(day_vals[d])
        last = dd
    if cur:
        events.append(statistics.fmean(cur))
    return events


def event_block(day_vals: dict[str, float], gap_days: int,
                boot: int, seed: int) -> dict:
    ev = cluster_panic_events(day_vals, gap_days)
    if len(ev) < 2:
        return {"gap_days": gap_days, "n_events": len(ev),
                "mean_bp": round(statistics.fmean(ev) * 100, 1) if ev else None,
                "win_pct": round(100 * sum(x > 0 for x in ev) / len(ev), 1)
                if ev else None, "event_t": None, "ci95_bp": None,
                "event_pass": False}
    by = {f"e{i}": [x] for i, x in enumerate(ev)}
    lo, hi = day_block_bootstrap(by, boot, seed)
    t = tstat(ev)
    # promote 跨源判据：>=10 个去相关独立恐慌事件、事件级 t>=2、CI 下界>0。
    return {"gap_days": gap_days, "n_events": len(ev),
            "mean_bp": round(statistics.fmean(ev) * 100, 1),
            "win_pct": round(100 * sum(x > 0 for x in ev) / len(ev), 1),
            "event_t": round(t, 2),
            "ci95_bp": [round(lo * 100, 1), round(hi * 100, 1)],
            "event_pass": bool(len(ev) >= 10 and t >= 2.0 and lo > 0)}


def dashboard(rows: list[dict], vol_leg: str, boot: int, seed: int) -> dict:
    sel = [r for r in rows if r["ext_pct"] <= EXT_MAX and r["rsi"] >= RSI_MIN
           and VOL_LEGS[vol_leg](r)]
    if not sel:
        return {"n": 0}
    vals = [r["fwd72_net"] for r in sel]
    by_day: dict[str, list[float]] = {}
    for r in sel:
        d = dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(r["fwd72_net"])
    days = [statistics.fmean(v) for v in by_day.values()]
    day_vals = {d: statistics.fmean(v) for d, v in by_day.items()}
    lo, hi = day_block_bootstrap(by_day, boot, seed)
    breadth, ncoin = coin_breadth(sel)
    panic_events = {f"gap{g}": event_block(day_vals, g, boot, seed)
                    for g in (1, 2, 3, 5)}
    return {
        "vol_leg": vol_leg,
        "n_episodes": len(sel),
        "n_signal_days": len(days),
        "mean_net_bp": round(statistics.fmean(vals) * 100, 1),
        "win_pct": round(100 * sum(x > 0 for x in vals) / len(vals), 1),
        "day_t": round(tstat(days), 2),
        "day_ci95_bp": [round(lo * 100, 1), round(hi * 100, 1)],
        "leave_one_month_out_worst_bp": leave_one_month_out_worst(sel),
        "coin_breadth_pos_pct": breadth,
        "n_coins": ncoin,
        "top5pct_profit_share": top5pct_concentration(sel),
        # 去相关"恐慌事件"口径：这才是跨源 promote 的有效样本（HL 复核证明按日
        # 仍被同波多币触发灌水）。主判据用 gap3（=72h 持仓窗）。
        "panic_events": panic_events,
        "gate_preliminary_pass": bool(
            tstat(days) >= 2.0 and lo > 0
            and statistics.fmean(vals) * 100 >= 3.0
            and (leave_one_month_out_worst(sel) or -1) > 0),
        # 事件级硬判据：>=10 个去相关独立恐慌事件、t>=2、CI 下界>0。
        "event_gate_pass": bool(panic_events["gap3"]["event_pass"]),
    }


def run_history(symbols, cache_dir, top_pcts, cost_bps, boot, seed) -> dict:
    out = {}
    for tp in top_pcts:
        rows = []
        for sym in symbols:
            p = os.path.join(cache_dir, f"{sym}_1h.json")
            if os.path.exists(p):
                rows.extend(episodes_with_atr_pctile(p, tp, cost_bps))
        out[str(tp)] = {leg: dashboard(rows, leg, boot, seed)
                        for leg in VOL_LEGS}
    return out


def run_jsonl(path, boot, seed) -> dict:
    """实时影子 jsonl：仅聚合已回填 outcome 的 is_candidate 行（前向收益缺失
    时跳过）。jsonl 当前不含 atr_pctile，波动腿只给 none，另两列置 n/a。"""
    rows = []
    total = pending = 0
    if not os.path.exists(path):
        return {"meta": {"jsonl": path, "total_lines": 0,
                         "candidate_outcome_pending": 0,
                         "note": "shadow jsonl not present yet; re-run after deploy"},
                "jsonl_live": {"none": {"n": 0}}}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if not rec.get("is_candidate"):
                continue
            net = (((rec.get("outcome") or {}).get("fwd72_net_pct"))
                   if isinstance(rec.get("outcome"), dict) else None)
            if net is None:
                pending += 1
                continue
            ts = int(rec.get("timestamp") or 0)
            rows.append({"ts": ts, "coin": rec.get("coin", "?"),
                         "ext_pct": rec.get("ext_pct", 0),
                         "rsi": rec.get("rsi14", 0),
                         "atr24_pct": rec.get("atr24_pct") or float("nan"),
                         "atr_pctile": rec.get("atr_pctile") or float("nan"),
                         "fwd72_net": float(net) / 100.0})
    dash = {"none": dashboard(rows, "none", boot, seed)}
    return {"meta": {"jsonl": path, "total_lines": total,
                     "candidate_outcome_pending": pending},
            "jsonl_live": dash}


def _print(title, payload) -> None:
    print("=" * 100)
    print(title)
    for tp, legs in payload.items():
        if tp == "meta":
            print(f"  meta: {legs}")
            continue
        print(f"  top_pct={tp}")
        if tp == "jsonl_live":
            legs = legs
        for leg, s in legs.items():
            if s.get("n_episodes", 0) == 0:
                print(f"    {leg:11} n=0")
                continue
            ev = s.get("panic_events", {}).get("gap3", {})
            print(f"    {leg:11} n={s['n_episodes']:3} days={s['n_signal_days']:3} "
                  f"mean={s['mean_net_bp']:7.1f}bp WR={s['win_pct']:5.1f}% "
                  f"t={s['day_t']:5.2f} CI={s['day_ci95_bp']} "
                  f"LOMOworst={s['leave_one_month_out_worst_bp']} "
                  f"breadth={s['coin_breadth_pos_pct']}%/{s['n_coins']}c "
                  f"top5%={s['top5pct_profit_share']} "
                  f"day:{'PASS' if s['gate_preliminary_pass'] else 'not-yet'}")
            print(f"    {'':11} panic-events(gap3): n={ev.get('n_events')} "
                  f"mean={ev.get('mean_bp')}bp t={ev.get('event_t')} "
                  f"CI={ev.get('ci95_bp')} "
                  f"EVENT:{'PASS' if s.get('event_gate_pass') else 'not-yet'}")
    print("=" * 100)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--top-pct", nargs="+", type=float, default=[85.0, 90.0])
    ap.add_argument("--cost-bps", type=float, default=9.26)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--jsonl", default=None, help="实时影子 jsonl 路径")
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    result = {}
    if args.jsonl:
        result["jsonl_live"] = run_jsonl(args.jsonl, args.boot, args.seed)
        _print(f"xs S2 dashboard — LIVE shadow jsonl {args.jsonl}",
               result["jsonl_live"])
    else:
        result["history"] = run_history(args.symbols, args.cache_dir,
                                        args.top_pct, args.cost_bps,
                                        args.boot, args.seed)
        _print("xs S2 dashboard — historical 1h (72h net, independent signal-days)",
               result["history"])
    if args.save:
        with open(args.save, "w") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
