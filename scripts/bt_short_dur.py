#!/usr/bin/env python3
"""阶段2-归因④：短空时长配对回测（含 HL 每小时资金费）。

纯统计深挖（基于 bt_nont_1h6 产物）发现 short 的 edge 只活在前 2 小时：
  trend regime short <2h：n=1783 净 +$52k 期望 +$29 胜66%（19/20 币正）
  trend regime short 2h+：净 -$189k 期望 -$53~-62 胜33%
short 做对时 floor_breach 单笔 +$207（与 long 的 +$199 同强），不是不会赚，而是
空头行情不持续、拖过 ~2h 被反弹系统性吃掉。现网 trend 时钟 stale=120m/hard=240m
边界偏松，大量短空拖进 2-4h 才被小亏平掉。

本脚本严格回答：**trend 短空强制在 X 分钟内了结，能挽回多少？会不会误杀走到
floor 的大赢单？相比"全关空"哪个更优？**
所有臂【信号/long 出场/non_trend 出场 100% 相同】，只对 trend regime 的 short
单改退出时钟：用 hard_timeout=X（到点无条件平）+ 关闭该腿 stale（让 hard 唯一
决定最长持仓），max_loss/protect/phase2 floor 全保持 prod。

臂：
  prod    现网（trend short hard240/stale120）
  sh120   trend short 最长 120m（hard120, stale0）
  sh90    90m
  sh60    60m
  short0  完全不做 trend/non 一切 short（long 同 prod）—— "全关空"对照

用法：
  python3 scripts/bt_short_dur.py --days 1250 \
      --coins BTC,...,TRX --write --out /tmp/hemo/shortdur.jsonl
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import bt_ra as B  # noqa: E402
import bt_clock_removal as C  # noqa: E402

MS_5M, MS_1H, MS_4H, MS_DAY = B.MS_5M, B.MS_1H, B.MS_4H, B.MS_DAY

# (arm, trend-short hard_min 或 None=全关 short)
ARMS = ["prod", "sh120", "sh90", "sh60", "short0"]
SHORT_HARD = {"prod": 240.0, "sh120": 120.0, "sh90": 90.0, "sh60": 60.0}
ARM_LABEL = {
    "prod": "现网 trend-short hard240/stale120",
    "sh120": "trend-short 硬上限120m",
    "sh90": "trend-short 硬上限90m",
    "sh60": "trend-short 硬上限60m",
    "short0": "全关 short（仅 long）",
}


def build_arm_dsls(P):
    """{arm: {"trend":..., "non_trend":...}}；DSL 是该臂 trend-short 专用。
    long 与 non_trend 在 replay 里始终用 prod DSL（见下），所以这里 trend DSL 仅
    用于 trend short 分叉；为简单起见返回 (trend_short_dsl, prod_dsl)。"""
    prod = C.build_dsls(P)["prod"]
    pt, nt = prod["trend"], prod["non_trend"]
    out = {}
    for arm in ARMS:
        if arm == "prod":
            out[arm] = (pt, nt, pt)            # short_dsl, non_dsl, long_dsl(prod)
        elif arm == "short0":
            out[arm] = (None, nt, pt)          # 不做 short
        else:
            hard = SHORT_HARD[arm]
            short_dsl = replace(pt, hard_timeout_minutes=hard,
                                stale_flat_timeout_minutes=0.0)
            out[arm] = (short_dsl, nt, pt)
    return out


def replay(coin, start_ms, end_ms, P, arm_dsls, notional, slips, funding):
    funnel = {k: 0 for k in (
        "bars", "fired_ge_1", "surfaced", "no_dir", "le_block", "hq_would",
        "sigma_would", "sigma_no_dir", "sigma_le_block", "runner_rsi_block",
        "skip_open_pos", "fetch_error")}
    out: List[dict] = []
    e_slip, x_slip, stop_delay = slips
    e_slip = B._slip_for(coin, e_slip, True)
    x_slip = B._slip_for(coin, x_slip, True)
    fetch = B._fetch_binance_chunked
    try:
        bars = fetch(coin, "5m", start_ms - 120 * MS_5M, end_ms, MS_5M)
        c1h_all = fetch(coin, "1h", start_ms - 60 * MS_1H, end_ms, MS_1H)
        c4h_all = fetch(coin, "4h", start_ms - 110 * MS_4H, end_ms, MS_4H)
        btc_1h = fetch("BTC", "1h", start_ms - 170 * MS_1H, end_ms, MS_1H)
    except Exception as e:
        print(f"  [fetch-error] {coin}: {e}")
        funnel["fetch_error"] += 1
        return out, funnel
    ts_1h = [c.t for c in c1h_all]
    ts_4h = [c.t for c in c4h_all]
    btc_ts = [c.t for c in btc_1h]
    rg = P["regime"]

    def regime_at(decision_ms):
        if len(btc_1h) < 100:
            return "neutral"
        k = bisect.bisect_right(btc_ts, decision_ms - MS_1H)
        if k < 100:
            return "neutral"
        try:
            return B.classify_candles(btc_1h[k - 100:k], fast_p=rg.get("fast_ema"),
                                     slow_p=rg.get("slow_ema"),
                                     slope_up=rg.get("slope_threshold"),
                                     adx_max=rg.get("chop_adx_max"))
        except Exception:
            return "neutral"

    if len(bars) < 60:
        return out, funnel
    open_until = {a: -1 for a in ARMS}
    ftimes = [t for t, _ in funding.get(coin, [])]
    frates = [r for _, r in funding.get(coin, [])]

    for i in range(len(bars)):
        if bars[i].t < start_ms or bars[i].t > end_ms or i + 1 >= len(bars):
            continue
        window = bars[max(0, i - 99):i + 1]
        if len(window) < 50:
            continue
        funnel["bars"] += 1
        decision_ms = bars[i].t + MS_5M
        c1h = B._closed_prefix(c1h_all, ts_1h, decision_ms, MS_1H, 48)
        c4h = B._closed_prefix(c4h_all, ts_4h, decision_ms, MS_4H, 100)
        macro = regime_at(decision_ms)
        cands = B._eval_bar(coin, window, c1h, c4h, P, funnel, macro)
        base = [c for c in cands if c.arm == "baseline"]
        if not base:
            continue
        cand = base[0]
        cand.bar_idx = i
        bucket = "trend" if macro in ("up", "down") else "non_trend"
        is_short = cand.side == "short"
        adx4h = cand.meta.get("adx4h")
        for arm in ARMS:
            if i <= open_until[arm]:
                funnel["skip_open_pos"] += 1
                continue
            short_dsl, non_dsl, long_dsl = arm_dsls[arm]
            if is_short and short_dsl is None:
                continue  # short0 臂：跳过一切空单
            if bucket == "trend":
                dsl = short_dsl if is_short else long_dsl
            else:
                # non_trend short：short0 也不做；其余臂 non 腿保持 prod
                if is_short:
                    dsl = None if arm == "short0" else non_dsl
                else:
                    dsl = non_dsl
                if dsl is None:
                    continue
            tr = B._simulate_trade(cand, bars, i, dsl, notional,
                                   e_slip, x_slip, stop_delay, coin)
            if tr is None:
                continue
            fc, nf = C.funding_cost_usd(ftimes, frates, tr.entry_t, tr.exit_t,
                                        tr.side, notional)
            open_until[arm] = i + tr.hold_bars
            out.append({
                "type": "trade", "arm": arm, "coin": coin, "side": tr.side,
                "regime_bucket": bucket, "macro_regime": macro,
                "entry_t": tr.entry_t, "exit_t": tr.exit_t,
                "exit_reason": tr.exit_reason,
                "hold_min": (tr.exit_t - tr.entry_t) / 60_000.0,
                "peak_pct": tr.peak_pct, "score": tr.score,
                "adx4h": (None if adx4h is None else float(adx4h)),
                "pnl_gross": tr.pnl_gross, "pnl_net": tr.pnl_net,
                "funding_usd": fc, "funding_n": nf,
                "pnl_nf": tr.pnl_net - fc,
            })
    return out, funnel


def summ(ts):
    n = len(ts)
    if not n:
        return {"n": 0}
    wins = [t for t in ts if t["pnl_nf"] > 0]
    net = sum(t["pnl_nf"] for t in ts)
    gl = -sum(t["pnl_nf"] for t in ts if t["pnl_nf"] <= 0)
    gw = sum(t["pnl_nf"] for t in wins)
    return {"n": n, "win": 100 * len(wins) / n, "net": net, "exp": net / n,
            "pf": (gw / gl if gl > 0 else float("inf")),
            "hold_h": sum(t["hold_min"] for t in ts) / 60 / n}


def _line(label, ts):
    if not ts:
        return f"    {label:<22s} n=0"
    s = summ(ts)
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    return (f"    {label:<22s} n={s['n']:>5d} 净${s['net']:>9.0f} "
            f"期望${s['exp']:>7.2f} 胜{s['win']:5.1f}% PF{pf:>5s} "
            f"均持{s['hold_h']:5.2f}h")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--days", type=int, default=1250)
    ap.add_argument("--coins", default=",".join(B.MAJORS))
    ap.add_argument("--end-ms", type=int, default=None)
    ap.add_argument("--notional", type=float, default=10_000.0)
    ap.add_argument("--fee-bps", type=float, default=B.ROUND_TRIP_FEE_BPS)
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default="/tmp/hemo/shortdur.jsonl")
    args = ap.parse_args()

    B.ROUND_TRIP_FEE_BPS = args.fee_bps
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cfg, cfg_src = B._load_config(args.config)
    P = B._resolve_params(cfg)
    arm_dsls = build_arm_dsls(P)

    now_ms = int(time.time() * 1000)
    end_ms = args.end_ms or (now_ms // MS_5M) * MS_5M - MS_5M
    start_ms = end_ms - args.days * MS_DAY
    print(f"config: {cfg_src}\ncoins: {','.join(coins)} days: {args.days}")
    for a in ARMS:
        print(f"  {a:7s} {ARM_LABEL[a]}")

    funding, coin_start_ms = {}, {}
    if not args.no_funding:
        for coin in coins:
            try:
                funding[coin] = C.fetch_funding_hourly(
                    coin, start_ms - MS_1H, end_ms + MS_1H)
                earliest = funding[coin][0][0] if funding[coin] else start_ms
                coin_start_ms[coin] = max(start_ms, earliest + MS_1H)
            except Exception as e:
                print(f"  [funding-warn] {coin}: {e!r}")
                funding[coin] = []
                coin_start_ms[coin] = start_ms
    else:
        coin_start_ms = {c: start_ms for c in coins}

    slips = (B.DEFAULT_ENTRY_SLIP_BPS, B.DEFAULT_EXIT_SLIP_BPS,
             B.DEFAULT_STOP_DELAY_SLIP_BPS)
    all_trades: List[dict] = []
    for coin in coins:
        c_start = coin_start_ms.get(coin, start_ms)
        print(f"\n[{coin}] since {time.strftime('%Y-%m-%d', time.gmtime(c_start/1000))}")
        t0 = time.time()
        ts, funnel = replay(coin, c_start, end_ms, P, arm_dsls,
                            args.notional, slips, funding)
        all_trades.extend(ts)
        nprod = sum(1 for t in ts if t["arm"] == "prod")
        print(f"  bars={funnel['bars']} trades={len(ts)} (prod {nprod}) {time.time()-t0:.1f}s")

    if args.no_funding:
        for t in all_trades:
            t["funding_usd"] = 0.0
            t["pnl_nf"] = t["pnl_net"]

    print("\n" + "=" * 96)
    print("臂级对照（含资金费净PnL）")
    print("=" * 96)
    for arm in ARMS:
        ts = [t for t in all_trades if t["arm"] == arm]
        s = summ(ts)
        if not s["n"]:
            continue
        pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"  {ARM_LABEL[arm]:<32s} n={s['n']:>5d} 胜{s['win']:>5.1f}% "
              f"期望${s['exp']:>7.2f} 净${s['net']:>11.0f} PF{pf:>5s} 均持{s['hold_h']:5.2f}h")

    print("\n" + "=" * 96)
    print("trend SHORT × 时长分段（核心）")
    print("=" * 96)
    for arm in ARMS:
        print(f"  [{arm}]")
        for lab, x, y in [("<1h", 0, 1), ("1-2h", 1, 2), ("2-4h", 2, 4), ("4h+", 4, 1e9)]:
            ts = [t for t in all_trades if t["arm"] == arm
                  and t["regime_bucket"] == "trend" and t["side"] == "short"
                  and x <= t["hold_min"] / 60 < y]
            if ts:
                print(_line(f"short/{lab}", ts))
        print(_line("trend-short合计",
                    [t for t in all_trades if t["arm"] == arm
                     and t["regime_bucket"] == "trend" and t["side"] == "short"]))

    print("\n" + "=" * 96)
    print("trend-short 退出原因迁移（硬上限是否误杀 floor 赢家）")
    print("=" * 96)
    reasons = ["floor_breach", "max_loss", "stale_flat_timeout",
               "hard_timeout", "end_of_data"]
    print(f"    {'arm':<8}" + "".join(f"{r:>20s}" for r in reasons))
    for arm in ARMS:
        cells = []
        for rsn in reasons:
            ts = [t for t in all_trades if t["arm"] == arm
                  and t["regime_bucket"] == "trend" and t["side"] == "short"
                  and t["exit_reason"] == rsn]
            cells.append(f"n{len(ts):>4}/${sum(t['pnl_nf'] for t in ts):>7.0f}" if ts
                         else f"{'--':>20s}")
        print(f"    {arm:<8}" + "".join(f"{c:>20s}" for c in cells))

    print("\n" + "=" * 96)
    print("Block bootstrap（按日总PnL 差 vs prod，10000 次）")
    print("=" * 96)
    import random
    random.seed(20261013)

    def daily(arm):
        d: Dict[int, float] = {}
        for t in all_trades:
            if t["arm"] == arm:
                d[t["entry_t"] // MS_DAY] = d.get(t["entry_t"] // MS_DAY, 0.0) + t["pnl_nf"]
        return d

    db = daily("prod")
    days = sorted(db)
    for arm in ("sh120", "sh90", "sh60", "short0"):
        dw = daily(arm)
        diffs = [dw.get(d, 0.0) - db.get(d, 0.0) for d in days]
        obs, nd = sum(diffs), len(diffs)
        boots = sorted(sum(diffs[random.randrange(nd)] for _ in range(nd))
                       for _ in range(10000))
        lo, hi = boots[250], boots[9750]
        verdict = "显著为正" if lo > 0 else ("显著为负" if hi < 0 else "不显著")
        pos = sum(1 for x in diffs if x > 0)
        print(f"  {arm:7s} Δ净=${obs:+.0f} over {nd}d (正日{pos}/{nd-pos}) "
              f"95%CI[${lo:+.0f},${hi:+.0f}] {verdict}")

    if args.write:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            f.write(json.dumps({"type": "run_meta", "config_src": cfg_src,
                                "coins": coins, "days": args.days,
                                "funding_on": not args.no_funding,
                                "notional": args.notional,
                                "fee_bps": args.fee_bps, "arms": ARMS}) + "\n")
            for t in all_trades:
                f.write(json.dumps(t) + "\n")
        print(f"\n[write] {len(all_trades)} trades -> {out_path}")


if __name__ == "__main__":
    main()
