#!/usr/bin/env python3
"""阶段2-归因②：stale 时钟出场配对回测（含 HL 每小时资金费）。

上一轮（bt_nont_1h6.py）定位到真正血口不是 non_trend 入场，而是 trend 单在
1-6h 被 stale_flat_timeout 拖死：prod 全样本 stale_flat 强平 n=14,104 / 净 -$592k
（trend 占 -$541k），这些单被强平时中位峰值浮盈仅 ~0.9%，始终走不到 protect/
结构地板；其中 trend/short 从 <1h 的 +$24k(胜64%) 拖到 1-6h 变 -$161k(胜39%)。

本脚本回答：**trend 单的 stale 时钟该不该缩短 / 改成更快的微利撤单？缩短到多少？**
所有臂【信号时刻 100% 相同、入场 100% 相同、non_trend 出场 100% 相同】，只在
"trend regime 单的退出时钟"上分叉，PnL 差可干净归因到 trend 出场时钟。

臂设计（non_trend 腿所有臂都用 prod DSL；只改 trend 腿）：
  prod   现网基准：trend stale=120m / hard=240m（不变）。
  st60   trend stale 120→60m：更早砍掉始终走不到 protect(2.5%) 的震荡单。
  st30   trend stale 120→30m。
  scr30  trend 关闭 stale，改 time_scratch=30m / min_peak=0.3%：30 分钟内峰值
         浮盈到不了 0.3% 就撤（一旦有微利则不被该规则平，交给阶梯）。
  st0    trend 完全关闭 stale（只靠 hard240/max_loss/phase2 floor），对照
         "不主动砍震荡单"。

资金费/数据源源与 bt_clock_removal.py 完全一致（直接复用）。

用法：
  python3 scripts/bt_stale_exit.py --days 1250 \
      --coins BTC,ETH,...,TRX --write --out /tmp/hemo/stale.jsonl
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import bt_ra as B  # noqa: E402
import bt_clock_removal as C  # noqa: E402  复用 funding/prod DSL/资金费

MS_5M = B.MS_5M
MS_1H = B.MS_1H
MS_4H = B.MS_4H
MS_DAY = B.MS_DAY

PROD_TREND_CLOCKS = C.PROD_TREND_CLOCKS    # {"hard":240,"stale":120}
PROD_NONTREND_CLOCKS = C.PROD_NONTREND_CLOCKS  # {"hard":120,"stale":60}

ARMS = ["prod", "st60", "st30", "scr30", "st0"]
ARM_LABEL = {
    "prod": "现网 trend stale120/hard240",
    "st60": "trend stale60",
    "st30": "trend stale30",
    "scr30": "trend scratch30/peak0.3(关stale)",
    "st0": "trend 关 stale",
}


def build_arm_dsls(P: Dict[str, Any]) -> Dict[str, Dict[str, B.DslParams]]:
    """{arm: {"trend": DslParams, "non_trend": DslParams}}。non_trend 全臂同 prod。"""
    prod = C.build_dsls(P)["prod"]
    nt = prod["non_trend"]                      # non_trend 腿：所有臂一致
    base_trend = prod["trend"]                  # 现网 trend 腿（stale120/hard240）

    def _trend(stale: float, hard: float,
               scratch_min: float = 0.0) -> B.DslParams:
        return replace(base_trend,
                       stale_flat_timeout_minutes=stale,
                       hard_timeout_minutes=hard,
                       time_scratch_minutes=scratch_min,
                       time_scratch_min_peak=(0.3 if scratch_min > 0 else 0.3))

    return {
        "prod": {"trend": base_trend, "non_trend": nt},
        "st60": {"trend": _trend(60.0, 240.0), "non_trend": nt},
        "st30": {"trend": _trend(30.0, 240.0), "non_trend": nt},
        # scratch：关闭 stale（0），开 time_scratch 30m；hard 仍 240
        "scr30": {"trend": replace(base_trend,
                                   stale_flat_timeout_minutes=0.0,
                                   hard_timeout_minutes=240.0,
                                   time_scratch_minutes=30.0,
                                   time_scratch_min_peak=0.3),
                  "non_trend": nt},
        "st0": {"trend": replace(base_trend,
                                 stale_flat_timeout_minutes=0.0,
                                 hard_timeout_minutes=240.0),
                "non_trend": nt},
    }


def replay(coin: str, start_ms: int, end_ms: int, P: Dict[str, Any],
           arm_dsls: Dict[str, Dict[str, B.DslParams]], notional: float,
           slips: Tuple[float, float, float], funding: Dict[str, List[Tuple[int, float]]]
           ) -> Tuple[List[dict], Dict[str, int]]:
    funnel = {
        "bars": 0, "fired_ge_1": 0, "surfaced": 0, "no_dir": 0,
        "le_block": 0, "hq_would": 0, "sigma_would": 0,
        "sigma_no_dir": 0, "sigma_le_block": 0,
        "runner_rsi_block": 0,
        "skip_open_pos": 0, "fetch_error": 0,
    }
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

    def regime_at(decision_ms: int) -> str:
        if len(btc_1h) < 100:
            return "neutral"
        k = bisect.bisect_right(btc_ts, decision_ms - MS_1H)
        if k < 100:
            return "neutral"
        sl = btc_1h[k - 100:k]
        try:
            return B.classify_candles(sl, fast_p=rg.get("fast_ema"),
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
        if bars[i].t < start_ms or bars[i].t > end_ms:
            continue
        if i + 1 >= len(bars):
            break
        window = bars[max(0, i - 99):i + 1]
        if len(window) < 50:
            continue
        funnel["bars"] += 1
        decision_ms = bars[i].t + MS_5M
        c1h = B._closed_prefix(c1h_all, ts_1h, decision_ms, MS_1H, 48)
        c4h = B._closed_prefix(c4h_all, ts_4h, decision_ms, MS_4H, 100)
        macro = regime_at(decision_ms)
        cands = B._eval_bar(coin, window, c1h, c4h, P, funnel, macro)
        base_cands = [c for c in cands if c.arm == "baseline"]
        if not base_cands:
            continue
        cand = base_cands[0]
        cand.bar_idx = i
        is_trend = macro in ("up", "down")
        bucket = "trend" if is_trend else "non_trend"
        adx4h = cand.meta.get("adx4h")
        for arm in ARMS:
            if i <= open_until[arm]:
                funnel["skip_open_pos"] += 1
                continue
            dsl = arm_dsls[arm][bucket]
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
                "entry_px": tr.entry_px, "exit_px": tr.exit_px,
                "exit_reason": tr.exit_reason,
                "hold_min": (tr.exit_t - tr.entry_t) / 60_000.0,
                "peak_pct": tr.peak_pct, "score": tr.score,
                "adx4h": (None if adx4h is None else float(adx4h)),
                "pnl_gross": tr.pnl_gross, "pnl_net": tr.pnl_net,
                "funding_usd": fc, "funding_n": nf,
                "pnl_nf": tr.pnl_net - fc,
            })
    return out, funnel


def summ(ts: List[dict], key: str = "pnl_nf") -> dict:
    n = len(ts)
    if not n:
        return {"n": 0}
    wins = [t for t in ts if t[key] > 0]
    net = sum(t[key] for t in ts)
    gl = -sum(t[key] for t in ts if t[key] <= 0)
    gw = sum(t[key] for t in wins)
    return {"n": n, "win": 100 * len(wins) / n, "net": net, "exp": net / n,
            "pf": (gw / gl if gl > 0 else float("inf")),
            "hold_h": sum(t["hold_min"] for t in ts) / 60.0 / n,
            "fund": sum(t["funding_usd"] for t in ts)}


HOLD_BUCKETS = [(0, 1, "<1h"), (1, 6, "1-6h"), (6, 24, "6-24h"),
                (24, 1e9, ">24h")]


def _line(label: str, ts: List[dict]) -> str:
    if not ts:
        return f"    {label:<24s} n=0"
    s = summ(ts)
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    return (f"    {label:<24s} n={s['n']:>5d} 净${s['net']:>10.0f} "
            f"期望${s['exp']:>7.2f} 胜{s['win']:5.1f}% PF{pf:>5s} "
            f"均持{s['hold_h']:5.2f}h")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--days", type=int, default=1250)
    ap.add_argument("--coins", default=",".join(B.MAJORS))
    ap.add_argument("--end-ms", type=int, default=None)
    ap.add_argument("--notional", type=float, default=10_000.0)
    ap.add_argument("--fee-bps", type=float, default=B.ROUND_TRIP_FEE_BPS)
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default="/tmp/hemo/stale.jsonl")
    args = ap.parse_args()

    B.ROUND_TRIP_FEE_BPS = args.fee_bps
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cfg, cfg_src = B._load_config(args.config)
    P = B._resolve_params(cfg)
    arm_dsls = build_arm_dsls(P)

    now_ms = int(time.time() * 1000)
    end_ms = args.end_ms or (now_ms // MS_5M) * MS_5M - MS_5M
    start_ms = end_ms - args.days * MS_DAY

    print(f"config: {cfg_src}")
    print(f"coins: {','.join(coins)}  days: {args.days}  notional: ${args.notional:,.0f}")
    print(f"fee round-trip: {args.fee_bps} bps  funding: "
          f"{'OFF' if args.no_funding else 'HL hourly ON'}")
    print("arms（信号/入场/non_trend出场全同，仅 trend 退出时钟不同）:")
    for a in ARMS:
        t = arm_dsls[a]["trend"]
        print(f"  {a:6s} {ARM_LABEL[a]:<34s} trend hard={t.hard_timeout_minutes:.0f} "
              f"stale={t.stale_flat_timeout_minutes:.0f} "
              f"scr={t.time_scratch_minutes:.0f}")

    funding: Dict[str, List[Tuple[int, float]]] = {}
    coin_start_ms: Dict[str, int] = {}
    if not args.no_funding:
        for coin in coins:
            t0 = time.time()
            try:
                funding[coin] = C.fetch_funding_hourly(
                    coin, start_ms - MS_1H, end_ms + MS_1H)
                earliest = funding[coin][0][0] if funding[coin] else start_ms
                coin_start_ms[coin] = max(start_ms, earliest + MS_1H)
                span_d = (funding[coin][-1][0] - earliest) / MS_DAY
                print(f"  funding {coin}: {len(funding[coin])} pts, "
                      f"{span_d:.0f}d since {time.strftime('%Y-%m-%d', time.gmtime(earliest/1000))} "
                      f"({time.time()-t0:.1f}s)")
            except Exception as e:
                print(f"  [funding-warn] {coin}: {e!r} — 资金费按 0 计")
                funding[coin] = []
                coin_start_ms[coin] = start_ms
    else:
        coin_start_ms = {c: start_ms for c in coins}

    slips = (B.DEFAULT_ENTRY_SLIP_BPS, B.DEFAULT_EXIT_SLIP_BPS,
             B.DEFAULT_STOP_DELAY_SLIP_BPS)
    all_trades: List[dict] = []
    for coin in coins:
        c_start = coin_start_ms.get(coin, start_ms)
        print(f"\n[{coin}] replaying since "
              f"{time.strftime('%Y-%m-%d', time.gmtime(c_start / 1000))} ...")
        t0 = time.time()
        ts, funnel = replay(coin, c_start, end_ms, P, arm_dsls,
                            args.notional, slips, funding)
        all_trades.extend(ts)
        nprod = sum(1 for t in ts if t["arm"] == "prod")
        print(f"  bars={funnel['bars']} trades={len(ts)} (prod {nprod}) "
              f"{time.time()-t0:.1f}s")

    if args.no_funding:
        for t in all_trades:
            t["funding_usd"] = 0.0
            t["pnl_nf"] = t["pnl_net"]

    # ── 臂级对照 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("臂级对照（pnl_nf = 扣费 + 含资金费）")
    print("=" * 96)
    for arm in ARMS:
        ts = [t for t in all_trades if t["arm"] == arm]
        s = summ(ts)
        if not s["n"]:
            print(f"  {ARM_LABEL[arm]:<34s} 0 笔")
            continue
        pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"  {ARM_LABEL[arm]:<34s} n={s['n']:>5d} 胜{s['win']:>5.1f}% "
              f"期望${s['exp']:>7.2f} 净${s['net']:>11.0f} PF{pf:>5s} "
              f"均持{s['hold_h']:>5.2f}h fund${s['fund']:>7.0f}")

    # ── 退出原因 × 臂（看 stale 缩短后钱是省了还是错过了 floor）──────────────
    print("\n" + "=" * 96)
    print("退出原因分解（含资金费净PnL）")
    print("=" * 96)
    reasons = sorted({t["exit_reason"] for t in all_trades})
    for arm in ARMS:
        print(f"  [{arm}] {ARM_LABEL[arm]}")
        for rsn in reasons:
            ts = [t for t in all_trades if t["arm"] == arm
                  and t["exit_reason"] == rsn]
            if not ts:
                continue
            print(_line(rsn, ts))

    # ── 核心：trend 单 × side × 时长分段（血口所在）─────────────────────────
    print("\n" + "=" * 96)
    print("trend 单 × side × 持仓时长（核心归因；non_trend 全臂相同不展示）")
    print("=" * 96)
    for arm in ARMS:
        print(f"  [{arm}] {ARM_LABEL[arm]}")
        for side in ("long", "short"):
            for lo, hi, hb in HOLD_BUCKETS:
                ts = [t for t in all_trades
                      if t["arm"] == arm and t["regime_bucket"] == "trend"
                      and t["side"] == side
                      and lo <= t["hold_min"] / 60.0 < hi]
                if ts:
                    print(_line(f"{side}/{hb}", ts))
        # trend 该臂合计
        print(_line("trend 合计",
                    [t for t in all_trades
                     if t["arm"] == arm and t["regime_bucket"] == "trend"]))

    # ── block bootstrap（各臂 vs prod，按日 Δ）──────────────────────────────
    print("\n" + "=" * 96)
    print("Block bootstrap（按日总PnL 差 vs prod，10000 次，95% CI）")
    print("=" * 96)
    import random
    random.seed(20261013)

    def daily(arm):
        d: Dict[int, float] = {}
        for t in all_trades:
            if t["arm"] == arm:
                d[t["entry_t"] // MS_DAY] = (
                    d.get(t["entry_t"] // MS_DAY, 0.0) + t["pnl_nf"])
        return d

    db = daily("prod")
    days = sorted(db)
    for arm in ("st60", "st30", "scr30", "st0"):
        dw = daily(arm)
        diffs = [dw.get(d, 0.0) - db.get(d, 0.0) for d in days]
        obs = sum(diffs)
        nd = len(diffs)
        boots = sorted(sum(diffs[random.randrange(nd)] for _ in range(nd))
                       for _ in range(10000))
        lo, hi = boots[250], boots[9750]
        verdict = "显著为正(该改)" if lo > 0 else (
            "显著为负(不该改)" if hi < 0 else "不显著")
        pos_days = sum(1 for x in diffs if x > 0)
        print(f"  {arm:6s} vs prod: Δ净=${obs:+.0f} over {nd}d "
              f"(正日{pos_days}/负日{nd-pos_days})  "
              f"95%CI [${lo:+.0f}, ${hi:+.0f}] → {verdict}")

    if args.write:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            f.write(json.dumps({"type": "run_meta", "config_src": cfg_src,
                                "coins": coins, "days": args.days,
                                "start_ms": start_ms, "end_ms": end_ms,
                                "funding_on": not args.no_funding,
                                "notional": args.notional,
                                "fee_bps": args.fee_bps,
                                "arms": ARMS}) + "\n")
            for t in all_trades:
                f.write(json.dumps(t) + "\n")
        print(f"\n[write] {len(all_trades)} trades -> {out_path}")


if __name__ == "__main__":
    main()
