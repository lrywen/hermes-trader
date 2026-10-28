#!/usr/bin/env python3
"""P1b 收口：xs 臂 top_pct 85(生产) vs 90(M1) 同信号流双计数（只读）。

背景：生产 xs_reversal.top_pct=85（尾部门 ext_percentile<=0.15），M1 验证口径
想用 90（<=0.10）。90 门集合是 85 门集合的**纯子集**，因此不需要跑两遍信号、
也不需要改实时链路：在同一条已过 85 门的去重叠 episode 流上，用连续的
ext_percentile 再切一刀即可同时还原两口径，并额外给出"过 85 但不过 90"的
反事实带（85-only）——它的 edge 才是"留在 85"相对"收紧到 90"多吃到/多承担
的部分，是定格 85 还是 90 的决定性证据。

两种数据源（都只读、record-only，绝不下单）：

  * 历史 1h（默认，时间记录优先历史）：直接复用生产判定原语 replay_coin +
    deoverlap，在 top_pct=85 的超集 episode 流上切 0.10 / (0.10,0.15]。
  * 实时影子 jsonl（--jsonl）：读 xs_reversal_shadow.jsonl，每行已含连续
    ext_percentile。生产样本未回填 72h outcome 前只给计数；已回填的行
    （outcome.fwd72_net_pct）才计入 edge，未回填单独计 pending。

口径与 xs_episode_stats / xs_s2_daily_dashboard 一致：72h 去重叠、按 UTC
信号日做日块 t / bootstrap 95% CI；fwd72_net 为百分数，×100 得 bp。

用法：
  python -m scripts.xs_top_pct_dual_count
  python -m scripts.xs_top_pct_dual_count --jsonl /data/xs_reversal_shadow.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
from typing import Optional

from scripts.xs_episode_stats import (
    day_block_bootstrap,
    tstat,
)
from scripts.xs_vol_gate_calibration import episodes_with_atr_pctile

TAIL_85 = (100.0 - 85.0) / 100.0   # 0.15
TAIL_90 = (100.0 - 90.0) / 100.0   # 0.10
COST_BPS_DEFAULT = 9.26
# 与 xs_exit_param_freeze / panic-gate record-only 完全相同的生产门内子集口径。
GATED_EXT_MAX, GATED_RSI_MIN, GATED_ATR_Q = -8.0, 20.0, 0.6


def _summarize(rows: list[dict], net_key: str, boot: int, seed: int) -> dict:
    """对一批 episode 给出去重叠后的计数与日块统计。net 为百分数 → bp ×100。"""
    if not rows:
        return {"n": 0, "days": 0, "mean_net_bp": None, "win_pct": None,
                "day_t": None, "day_ci95_bp": None}
    vals = [r[net_key] for r in rows]
    by_day: dict[str, list[float]] = {}
    for r in rows:
        d = dt.datetime.utcfromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(r[net_key])
    days = [statistics.fmean(v) for v in by_day.values()]
    lo, hi = day_block_bootstrap(by_day, boot, seed)
    return {
        "n": len(rows),
        "days": len(days),
        "mean_net_bp": round(statistics.fmean(vals) * 100.0, 1),
        "win_pct": round(100.0 * sum(1 for x in vals if x > 0) / len(vals), 1),
        "day_t": round(tstat(days), 2) if len(days) >= 2 else None,
        "day_ci95_bp": [round(lo * 100.0, 1), round(hi * 100.0, 1)]
        if lo == lo else None,
    }


def _bands(rows: list[dict], boot: int, seed: int) -> dict:
    """在同一条 85-超集 episode 流上按连续 pctile 切出 90 子集与 85-only 反事实带。"""
    band90 = [r for r in rows if r["pctile"] <= TAIL_90]
    band85only = [r for r in rows if TAIL_90 < r["pctile"] <= TAIL_85]
    return {"all85_triggers": _summarize(rows, "fwd72_net", boot, seed),
            "top90_subset": _summarize(band90, "fwd72_net", boot, seed),
            "band_85_only": _summarize(band85only, "fwd72_net", boot, seed)}


def history_dual(symbols: list[str], cache_dir: str, cost_bps: float,
                 boot: int, seed: int, gated_only: bool = False) -> dict:
    """在 85 超集 episode 流上切 90 / 85-only，全部同流、同去重叠。

    episodes_with_atr_pctile 以 top_pct=85 取超集并附 atr_pctile（透传 replay_coin
    的 pctile / fwd72_net）。同时给全候选面板与生产 panic-gated 子集面板，后者与
    xs_exit_param_freeze 的 n=98 集合同口径，使 85/90 定格可直接对照退出冻结证据。
    """
    superset: list[dict] = []
    used: list[str] = []
    for sym in symbols:
        path = os.path.join(cache_dir, f"{sym}_1h.json")
        if not os.path.exists(path):
            continue
        rows = episodes_with_atr_pctile(path, 85.0, cost_bps)
        superset.extend(rows)
        used.append(sym)
    gated = [r for r in superset
             if r["ext_pct"] <= GATED_EXT_MAX and r["rsi"] >= GATED_RSI_MIN
             and r["atr_pctile"] >= GATED_ATR_Q]
    result = {
        "source": "history_1h",
        "symbols": used,
        "cost_bps_rt": cost_bps,
        "tail_cut_85": TAIL_85, "tail_cut_90": TAIL_90,
        "gated_filter": {"ext_pct_max": GATED_EXT_MAX, "rsi_min": GATED_RSI_MIN,
                         "atr_pctile_min": GATED_ATR_Q},
        "gated": _bands(gated, boot, seed),
        "note": ("90 is a strict subset of 85 on the SAME de-overlapped stream; "
                 "band_85_only is the counterfactual kept by 85 but dropped by 90. "
                 "Freeze 85 if that band stays positive; tighten to 90 only if it "
                 "is negative and drags the mean below the 90 subset."),
    }
    if not gated_only:
        result["all_candidates"] = _bands(superset, boot, seed)
    return result


def jsonl_dual(path: str, boot: int, seed: int, days: Optional[float]) -> dict:
    """实时影子 jsonl：用 ext_percentile 双切。edge 仅计已回填 outcome 的行。"""
    if not os.path.exists(path):
        return {"source": "live_shadow_jsonl", "file": path, "rows": 0,
                "note": "shadow jsonl not present yet; re-run after deploy"}
    cutoff = None
    if days:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=days)).timestamp() * 1000
    total = 0
    counts = {"all85_rows": 0, "top90_rows": 0, "band85_only_rows": 0,
              "cand85": 0, "cand90": 0, "cand85only": 0}
    pending = {"p90": 0, "p85only": 0}
    rows90: list[dict] = []
    rows85only: list[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = int(rec.get("timestamp") or 0)
            if cutoff is not None and ts < cutoff:
                continue
            p = rec.get("ext_percentile")
            if not isinstance(p, (int, float)):
                continue
            total += 1
            in85 = p <= TAIL_85
            in90 = p <= TAIL_90
            if in85:
                counts["all85_rows"] += 1
            if in90:
                counts["top90_rows"] += 1
            if in85 and not in90:
                counts["band85_only_rows"] += 1
            if not rec.get("is_candidate"):
                continue
            # 候选门（downtrend ∧ RSI）通过后才是潜在交易，edge 才可比。
            if in85:
                counts["cand85"] += 1
            if in90:
                counts["cand90"] += 1
            if in85 and not in90:
                counts["cand85only"] += 1
            out = rec.get("outcome")
            net = out.get("fwd72_net_pct") if isinstance(out, dict) else None
            band = "90" if in90 else ("85only" if in85 else None)
            if band is None:
                continue
            if net is None:
                pending["p90" if band == "90" else "p85only"] += 1
                continue
            row = {"ts": ts, "fwd72_net": float(net) / 100.0}
            (rows90 if band == "90" else rows85only).append(row)
    return {
        "source": "live_shadow_jsonl",
        "file": path,
        "window_days": days,
        "rows_with_percentile": total,
        **counts,
        "outcome_pending": pending,
        "top90_subset": _summarize(rows90, "fwd72_net", boot, seed),
        "band_85_only": _summarize(rows85only, "fwd72_net", boot, seed),
        "note": ("record-only; 90 ⊂ 85. Edge columns cover only rows with a "
                 "backfilled 72h outcome (pending rows excluded). Do not change "
                 "production top_pct=85 until the live 85-only band matches the "
                 "historical sign with enough independent signal-days."),
    }


def _print_block(title: str, bands: dict) -> None:
    print("-" * 92)
    print(title)
    for key in ("all85_triggers", "top90_subset", "band_85_only"):
        r = bands.get(key)
        if not r or not r.get("n"):
            print(f"  {key:16} n=0")
            continue
        print(f"  {key:16} n={r['n']:4} days={r['days']:4} "
              f"mean={r['mean_net_bp']:8.1f}bp WR={r['win_pct']:5.1f}% "
              f"t={str(r['day_t']):>6} CI={r['day_ci95_bp']}")


def _print_history(result: dict) -> None:
    print("=" * 92)
    print("xs top_pct 85 vs 90 — same-stream dual count (historical 1h, 72h net)")
    print(f"symbols={result['symbols']} cost={result['cost_bps_rt']}bp rt")
    _print_block("PRODUCTION panic-gated subset (ext<=-8, RSI>=20, atrP>=0.6)",
                 result["gated"])
    if "all_candidates" in result:
        _print_block("ALL xs candidates (downtrend + RSI[15,35), no vol gate)",
                     result["all_candidates"])
    print("=" * 92)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--cache-dir", default="logs/binance_klines_cache")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS_DEFAULT)
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20261028)
    ap.add_argument("--jsonl", default=None,
                    help="实时影子 xs_reversal_shadow.jsonl 路径（缺省走历史 K 线）")
    ap.add_argument("--days", type=float, default=None,
                    help="--jsonl 模式只统计最近 N 天")
    ap.add_argument("--gated-only", action="store_true",
                    help="历史模式只输出 panic-gated 子集面板")
    ap.add_argument("--save", default=None)
    args = ap.parse_args(argv)

    if args.jsonl:
        out_json = jsonl_dual(args.jsonl, args.boot, args.seed, args.days)
        print(json.dumps(out_json, ensure_ascii=False, indent=2))
    else:
        out_json = history_dual(args.symbols, args.cache_dir, args.cost_bps,
                                args.boot, args.seed, args.gated_only)
        _print_history(out_json)
    if args.save:
        with open(args.save, "w") as fh:
            json.dump(out_json, fh, ensure_ascii=False, indent=2)
        print(f"saved -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
