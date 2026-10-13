#!/usr/bin/env python3
"""阶段2-归因：non_trend 入场质量 + 1-6h 失血桶 配对回测（含 HL 每小时资金费）。

上一轮 20 币 × 1250 天拉满回测（bt_clock_removal.py）证伪了"长持 edge"：
拆时钟三臂全亏，唯一稳定为正的格子是 trend  regime 的 <1h 快进快出；最大失血
桶是 prod 的 1-6h（净 -$273k），且 non_trend 单持续放血。本脚本回答两个问题：

  Q1  non_trend 入场到底该不该做？收紧哪一刀最有效？
  Q2  1-6h 失血桶里，亏损集中在什么 regime/side/强度（adx4h）的单子？

所有臂【共用完全相同的 prod 出场 DSL】（入场时按 regime 冻结时钟），只在
"入场是否放行"上分叉 —— 因此臂间 PnL 差可干净归因到入场过滤，不被出场差异污染。

臂设计（信号时刻 100% 相同，出场 100% 相同，只改入场放行）：
  prod      现网基准：所有 baseline 候选都做（trend/non_trend 同口径）。
  nt0       完全剔除 non_trend 单（只做 BTC 代理 regime ∈ up/down 的单）。
            隔离 non_trend 入场的总贡献（应为正即说明该剔除）。
  ntADX25   non_trend 单要求 adx4h >= 25 才做（trend 单不受影响）。
  ntADX30   non_trend 单要求 adx4h >= 30 才做。
  ntLO      non_trend 只留多单（剔除 non_trend 空单；trend 单不受影响）。

每笔额外落 adx4h / rsi4h / score / extension，供 1-6h 桶按强度归因。

资金费/数据源源与 bt_clock_removal.py 完全一致（直接复用其函数）。

用法：
  python3 scripts/bt_nont_1h6.py --days 1250 \
      --coins BTC,ETH,...,TRX --write --out /tmp/hemo/nont.jsonl
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import bt_ra as B  # noqa: E402
import bt_clock_removal as C  # noqa: E402  复用 funding/regime冻结DSL/资金费

MS_5M = B.MS_5M
MS_1H = B.MS_1H
MS_4H = B.MS_4H
MS_DAY = B.MS_DAY

# 入场过滤臂（只作用于 non_trend 候选；"prod"/"nt0" 是特例）
NT_ADX_FLOOR = {"ntADX25": 25.0, "ntADX30": 30.0}
ARMS = ["prod", "nt0", "ntADX25", "ntADX30", "ntLO"]
ARM_LABEL = {
    "prod": "现网基准（全做）",
    "nt0": "完全剔除 non_trend",
    "ntADX25": "non_trend 需 adx4h>=25",
    "ntADX30": "non_trend 需 adx4h>=30",
    "ntLO": "non_trend 只留多单",
}


def admit(arm: str, bucket: str, side: str, adx4h: Optional[float]) -> bool:
    """该臂是否放行此候选。trend 单所有臂都放行（隔离 non_trend 入场效果）。"""
    if bucket == "trend":
        return True
    # non_trend
    if arm == "prod":
        return True
    if arm == "nt0":
        return False
    if arm in NT_ADX_FLOOR:
        # adx4h 缺失 fail-open（与弱趋势禁追生产口径一致：不拦无数据单）
        return adx4h is not None and float(adx4h) >= NT_ADX_FLOOR[arm]
    if arm == "ntLO":
        return side == "long"
    return True


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
        rsi4h = cand.meta.get("rsi4h")
        extension = cand.meta.get("extension")
        for arm in ARMS:
            if i <= open_until[arm]:
                funnel["skip_open_pos"] += 1
                continue
            if not admit(arm, bucket, cand.side, adx4h):
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
                "rsi4h": (None if rsi4h is None else float(rsi4h)),
                "extension": (None if extension is None else float(extension)),
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
ADX_BINS = [(0, 15, "adx<15"), (15, 20, "15-20"), (20, 25, "20-25"),
            (25, 30, "25-30"), (30, 1000, "adx>=30")]


def _line(label: str, ts: List[dict]) -> str:
    if not ts:
        return f"    {label:<22s} n=0"
    s = summ(ts)
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    return (f"    {label:<22s} n={s['n']:>5d} 净${s['net']:>10.0f} "
            f"期望${s['exp']:>7.2f} 胜{s['win']:5.1f}% PF{pf:>5s}")


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
    ap.add_argument("--out", default="/tmp/hemo/nont.jsonl")
    args = ap.parse_args()

    B.ROUND_TRIP_FEE_BPS = args.fee_bps
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cfg, cfg_src = B._load_config(args.config)
    P = B._resolve_params(cfg)
    # 所有臂共用同一套 prod regime 冻结 DSL（build_dsls["prod"]），出场零差异
    prod_dsl = C.build_dsls(P)["prod"]
    arm_dsls = {a: prod_dsl for a in ARMS}

    now_ms = int(time.time() * 1000)
    end_ms = args.end_ms or (now_ms // MS_5M) * MS_5M - MS_5M
    start_ms = end_ms - args.days * MS_DAY

    print(f"config: {cfg_src}")
    print(f"coins: {','.join(coins)}  days: {args.days}  notional: ${args.notional:,.0f}")
    print(f"fee round-trip: {args.fee_bps} bps  funding: "
          f"{'OFF' if args.no_funding else 'HL hourly ON'}")
    print("arms（出场全用 prod DSL，仅入场放行不同）:")
    for a in ARMS:
        print(f"  {a:8s} {ARM_LABEL[a]}")

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

    # ── Q1 臂级对照 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("Q1 臂级对照（pnl_nf = 扣费 + 含资金费；出场全同，差异=入场过滤）")
    print("=" * 96)
    for arm in ARMS:
        ts = [t for t in all_trades if t["arm"] == arm]
        s = summ(ts)
        if not s["n"]:
            print(f"  {ARM_LABEL[arm]:<28s} 0 笔")
            continue
        pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"  {ARM_LABEL[arm]:<28s} n={s['n']:>5d} 胜{s['win']:>5.1f}% "
              f"期望${s['exp']:>7.2f} 净${s['net']:>11.0f} PF{pf:>5s} "
              f"均持{s['hold_h']:>5.1f}h fund${s['fund']:>7.0f}")

    # ── 被剔除子集表现（关键：被过滤掉的单是否真的是烂单）────────────────────
    print("\n" + "=" * 96)
    print("被各刀剔除的 non_trend 单【在 prod 口径下】的表现（应为负才值得剔）")
    print("=" * 96)
    prod_nt = {(t["coin"], t["entry_t"], t["side"]): t
               for t in all_trades
               if t["arm"] == "prod" and t["regime_bucket"] == "non_trend"}
    kept = {a: set() for a in ARMS}
    for t in all_trades:
        if t["regime_bucket"] == "non_trend":
            kept[t["arm"]].add((t["coin"], t["entry_t"], t["side"]))
    for arm in ("nt0", "ntADX25", "ntADX30", "ntLO"):
        removed = [t for k, t in prod_nt.items() if k not in kept[arm]]
        n_long = sum(1 for t in removed if t["side"] == "long")
        print(f"  [{arm}] 剔除 {len(removed)} 笔 (long {n_long}/short "
              f"{len(removed)-n_long}):")
        print(_line("all removed", removed))
        print(_line("  long", [t for t in removed if t["side"] == "long"]))
        print(_line("  short", [t for t in removed if t["side"] == "short"]))

    # ── Q1b non_trend 单按 adx4h 分档（找最佳切刀位置）──────────────────────
    print("\n" + "=" * 96)
    print("prod 口径下 non_trend 单按 adx4h 强度分档（找收紧切点）")
    print("=" * 96)
    for lo, hi, lab in ADX_BINS:
        ts = [t for t in prod_nt.values()
              if t["adx4h"] is not None and lo <= t["adx4h"] < hi]
        print(_line(lab, ts))
    print(_line("adx 缺失", [t for t in prod_nt.values() if t["adx4h"] is None]))

    # ── Q2 1-6h 失血桶画像（prod）──────────────────────────────────────────
    print("\n" + "=" * 96)
    print("Q2 prod 1-6h 失血桶归因（净 PnL）")
    print("=" * 96)
    prod_all = [t for t in all_trades if t["arm"] == "prod"]
    b16 = [t for t in prod_all if 1 <= t["hold_min"] / 60.0 < 6]
    print(f"  1-6h 合计 n={len(b16)} 净${sum(t['pnl_nf'] for t in b16):.0f}")
    print(_line("trend", [t for t in b16 if t["regime_bucket"] == "trend"]))
    print(_line("non_trend", [t for t in b16 if t["regime_bucket"] == "non_trend"]))
    print(_line("long", [t for t in b16 if t["side"] == "long"]))
    print(_line("short", [t for t in b16 if t["side"] == "short"]))
    print(_line("trend/long", [t for t in b16 if t["regime_bucket"] == "trend"
                               and t["side"] == "long"]))
    print(_line("trend/short", [t for t in b16 if t["regime_bucket"] == "trend"
                                and t["side"] == "short"]))
    print(_line("non/long", [t for t in b16 if t["regime_bucket"] == "non_trend"
                             and t["side"] == "long"]))
    print(_line("non/short", [t for t in b16 if t["regime_bucket"] == "non_trend"
                              and t["side"] == "short"]))
    print("  -- 1-6h × 退出原因 --")
    for rsn in sorted({t["exit_reason"] for t in b16}):
        print(_line(rsn, [t for t in b16 if t["exit_reason"] == rsn]))
    print("  -- 1-6h × adx4h 分档 --")
    for lo, hi, lab in ADX_BINS:
        ts = [t for t in b16 if t["adx4h"] is not None and lo <= t["adx4h"] < hi]
        print(_line(lab, ts))

    # ── 时长分段 × 臂（确认过滤不损伤 <1h trend 正 edge）────────────────────
    print("\n" + "=" * 96)
    print("持仓时长分段 × 臂（含资金费）")
    print("=" * 96)
    for arm in ARMS:
        print(f"  [{arm}] {ARM_LABEL[arm]}")
        for lo, hi, label in HOLD_BUCKETS:
            ts = [t for t in all_trades if t["arm"] == arm
                  and lo <= t["hold_min"] / 60.0 < hi]
            print(_line(label, ts))

    # ── block bootstrap（各过滤臂 vs prod，按日 Δ）─────────────────────────
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
    for arm in ("nt0", "ntADX25", "ntADX30", "ntLO"):
        dw = daily(arm)
        diffs = [dw.get(d, 0.0) - db.get(d, 0.0) for d in days]
        obs = sum(diffs)
        nd = len(diffs)
        boots = sorted(sum(diffs[random.randrange(nd)] for _ in range(nd))
                       for _ in range(10000))
        lo, hi = boots[250], boots[9750]
        verdict = "显著为正(该剔)" if lo > 0 else (
            "显著为负(不该剔)" if hi < 0 else "不显著")
        print(f"  {arm:8s} vs prod: Δ净=${obs:+.0f} over {nd}d  "
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
