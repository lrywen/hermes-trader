#!/usr/bin/env python3
"""阶段0+1：日内时钟拆除 配对回测（含 HL 每小时资金费）。

回答一个问题：**不限持仓周期、让行情决定离场，长持到底有没有 edge？edge
来自哪几段持仓时长？** 所有臂共享同一信号时刻（严格配对），只改"出场 DSL"。

臂设计（信号 100% 相同，只改 DSL）：
  prod      生产如真：入场时按【入场 regime 冻结】时钟与 stop/protect
            （trend hard240/stale120/stop4.0/ride2.5；non hard120/stale60/
             stop1.5/scalp1.5）—— 这是当前线上的忠实复刻，对照基准。
  h600      仅放宽时钟：hard=600min(10h)，stale 关闭，stop/protect 仍随入场
            regime（与 prod 同）。隔离"放宽时钟"的纯效果。
  hold      彻底拆时钟：hard=30天灾难兜底，stale/ignite 关闭，stop/protect
            仍随入场 regime。隔离"近乎不限时 + trailing/结构地板"的效果。

资金费：Hyperliquid 永续【每小时】结算。持仓区间内每个 funding 时刻按
  sign(side) 收费：long 支付 +rate、short 收取 -rate。rate 来自 HL
  fundingHistory（与实盘同一交易所；K 线为 Binance 现货，资金费是最佳可得
  代理，差异在报告中注明）。对每笔交易同时输出 pnl_net（无 funding）与
  pnl_nf（含 funding），使 funding 的边际贡献可审计。

数据源：scripts/bt_ra.py 的 K 线抓取/信号评估/DSL 阶梯模拟（直接 import 复用，
不重复实现），仅在其 Trade/closing 之外另算 funding 与 regime 冻结时钟。

用法：
  python3 scripts/bt_clock_removal.py --days 180 --write --out /tmp/hemo/clock.jsonl
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import bt_ra as B  # noqa: E402  复用 K线/信号/DSL 模拟

MS_5M = B.MS_5M
MS_1H = B.MS_1H
MS_4H = B.MS_4H
MS_DAY = B.MS_DAY

# 生产 regime_aware.clocks / max_loss（config_defaults dsl_exit.regime_aware）
PROD_TREND_CLOCKS = {"hard": 240.0, "stale": 120.0}
PROD_NONTREND_CLOCKS = {"hard": 120.0, "stale": 60.0}
DISASTER_HARD_MIN = 30.0 * 24 * 60.0   # hold 臂：30 天灾难兜底（不参与正常退出）

_FUND_CACHE_DIR = _REPO / "logs" / "funding_cache"
_HL_INFO = "https://api.hyperliquid.xyz/info"


# ─────────────────────────────────────────────────────────────────────────────
# HL 每小时资金费（分页向前，每页 500 根≈20.8 天；磁盘缓存）
# ─────────────────────────────────────────────────────────────────────────────
def fetch_funding_hourly(coin: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
    """返回 [(fundingTime_ms, rate_per_hour_fraction), ...] 升序、去重、含缓存。

    HL fundingHistory：给定 startTime 返回其【之后最老的】≤500 根（最老优先）。
    向前补：startTime=上一页最新+1；向后补更早历史：startTime=目标更早点，
    返回的 500 根覆盖 [startTime, startTime+~21d)，据此把窗口向左推进。
    """
    _FUND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = _FUND_CACHE_DIR / f"{coin}.json"
    rows: Dict[int, float] = {}
    if cpath.is_file():
        try:
            for t, r in json.loads(cpath.read_text()):
                rows[int(t)] = float(r)
        except Exception:
            rows = {}

    def _page(st: int) -> list:
        body = json.dumps({"type": "fundingHistory", "coin": coin,
                           "startTime": st}).encode()
        for attempt in range(4):  # 瞬时 SSL/握手超时重试，退避
            try:
                req = urllib.request.Request(
                    _HL_INFO, data=body,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "curl/8"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    p = json.loads(r.read())
                return p if isinstance(p, list) else []
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return []

    have = sorted(rows)
    covered_left = have and have[0] <= start_ms
    covered_right = have and have[-1] >= end_ms - MS_1H
    if not (covered_left and covered_right):
        # 1) 向前（向新）补到 end_ms
        cur = (have[-1] + 1) if have else start_ms
        guard = 0
        while cur < end_ms and guard < 200:
            page = _page(cur)
            if not page:
                break
            for x in page:
                rows[int(x["time"])] = float(x["fundingRate"])
            newest = max(int(x["time"]) for x in page)
            if newest <= cur:
                break
            cur = newest + 1
            guard += 1
            time.sleep(0.05)
        # 2) 向后（向更早）补到 start_ms：每次取一个 ~21 天窗口的起点
        guard = 0
        while guard < 200:
            have = sorted(rows)
            if have and have[0] <= start_ms:
                break
            anchor = have[0] if have else end_ms
            target = anchor - 21 * MS_DAY  # 500 小时 ≈ 20.8 天
            page = _page(max(0, target))
            if not page:
                break
            for x in page:
                rows[int(x["time"])] = float(x["fundingRate"])
            oldest = min(int(x["time"]) for x in page)
            if oldest >= anchor or oldest <= start_ms:
                break
            guard += 1
            time.sleep(0.05)
        cpath.write_text(json.dumps([(t, rows[t]) for t in sorted(rows)]))
    out = [(t, r) for t, r in sorted(rows.items()) if start_ms <= t <= end_ms]
    return out


def funding_cost_usd(ftimes: List[int], rates: List[float],
                     entry_t: int, exit_t: int, side: str,
                     notional: float) -> Tuple[float, int]:
    """持仓 (entry_t, exit_t] 内结算的资金费（USD）。

    约定：funding 时刻 t 落在 (entry, exit] 则该笔计入本次持仓。
    long 在 rate>0 时付费（成本为正）；short 收 -rate。返回 (成本USD, 结算笔数)。
    """
    lo = bisect.bisect_right(ftimes, entry_t)
    hi = bisect.bisect_right(ftimes, exit_t)
    acc = sum(rates[k] for k in range(lo, hi))
    sgn = 1.0 if side == "long" else -1.0
    return sgn * acc * notional, (hi - lo)


# ─────────────────────────────────────────────────────────────────────────────
# 臂 → 入场 regime 冻结的 DSL（复刻 executor.select_exit_params + resolve_regime_clocks）
# ─────────────────────────────────────────────────────────────────────────────
def build_dsls(P: Dict[str, Any]) -> Dict[str, Dict[str, B.DslParams]]:
    """返回 {arm: {"trend": DslParams, "non_trend": DslParams}}。"""
    dx = P["dsl"]
    ra = dx.get("regime_aware", {}) or {}
    tr = ra.get("trend_ride", {}) or {}
    ml = ra.get("max_loss", {}) or {}
    tr_ml = ml.get("trend", {}) or {}
    nt_ml = ml.get("non_trend", {}) or {}
    base = B.DslParams.from_config(dx)
    tr_tiers = sorted(
        (float(t["pct_above_entry"]), float(t["retrace_threshold"]))
        for t in (tr.get("phase2_tiers") or [])) or None

    def _mk(hard: float, stale: float, trend: bool) -> B.DslParams:
        if trend:
            return B.DslParams(
                max_loss_pct=float(tr_ml.get("max_loss_pct", 4.0)),
                protect_pct=float(tr.get("protect_pct", 2.5)),
                retrace_threshold=float(tr.get("retrace_threshold", 0.4)),
                hard_timeout_minutes=hard,
                breakeven_trigger_pct=float(dx.get("breakeven_trigger_pct", 2.5)),
                breakeven_lock_pct=float(dx.get("breakeven_lock_pct", 0.5)),
                stale_flat_timeout_minutes=stale,
                phase2_tiers=tr_tiers or base.phase2_tiers)
        return replace(base,
                       max_loss_pct=float(nt_ml.get("max_loss_pct", 1.5)),
                       hard_timeout_minutes=hard,
                       stale_flat_timeout_minutes=stale)

    return {
        "prod": {"trend": _mk(PROD_TREND_CLOCKS["hard"], PROD_TREND_CLOCKS["stale"], True),
                 "non_trend": _mk(PROD_NONTREND_CLOCKS["hard"],
                                  PROD_NONTREND_CLOCKS["stale"], False)},
        "h600": {"trend": _mk(600.0, 0.0, True),
                 "non_trend": _mk(600.0, 0.0, False)},
        "hold": {"trend": _mk(DISASTER_HARD_MIN, 0.0, True),
                 "non_trend": _mk(DISASTER_HARD_MIN, 0.0, False)},
    }


ARMS = ["prod", "h600", "hold"]
ARM_LABEL = {"prod": "生产 regime时钟(240/120·120/60)",
             "h600": "放宽时钟 hard10h/stale关",
             "hold": "拆时钟 hard30d兜底/stale关"}


# ─────────────────────────────────────────────────────────────────────────────
# 单币回放：与 bt_ra.replay_coin 同一候选生成，但每臂按入场 regime 冻结 DSL，
# 成交后追加 funding 成本。
# ─────────────────────────────────────────────────────────────────────────────
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

    # 每臂独立"持仓覆盖到的 bar idx"，与 bt_ra 同一仓位互斥规则
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
        cands = B._eval_bar(coin, window, c1h, c4h, P, funnel,
                            regime_at(bars[i].t + MS_5M))
        base_cands = [c for c in cands if c.arm == "baseline"]
        if not base_cands:
            continue
        cand = base_cands[0]
        cand.bar_idx = i
        is_trend = regime_at(bars[i].t + MS_5M) in ("up", "down")
        bucket = "trend" if is_trend else "non_trend"
        for arm in ARMS:
            if i <= open_until[arm]:
                funnel["skip_open_pos"] += 1
                continue
            dsl = arm_dsls[arm][bucket]
            tr = B._simulate_trade(cand, bars, i, dsl, notional,
                                   e_slip, x_slip, stop_delay, coin)
            if tr is None:
                continue
            fc, nf = funding_cost_usd(ftimes, frates, tr.entry_t, tr.exit_t,
                                      tr.side, notional)
            open_until[arm] = i + tr.hold_bars
            out.append({
                "type": "trade", "arm": arm, "coin": coin, "side": tr.side,
                "regime_bucket": bucket, "entry_t": tr.entry_t,
                "exit_t": tr.exit_t, "entry_px": tr.entry_px,
                "exit_px": tr.exit_px, "exit_reason": tr.exit_reason,
                "hold_min": (tr.exit_t - tr.entry_t) / 60_000.0,
                "peak_pct": tr.peak_pct, "score": tr.score,
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--coins", default=",".join(B.MAJORS))
    ap.add_argument("--end-ms", type=int, default=None)
    ap.add_argument("--notional", type=float, default=10_000.0)
    ap.add_argument("--fee-bps", type=float, default=B.ROUND_TRIP_FEE_BPS)
    ap.add_argument("--no-funding", action="store_true",
                    help="资金费置零（用于对照 funding 的边际影响）")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default="/tmp/hemo/clock.jsonl")
    args = ap.parse_args()

    B.ROUND_TRIP_FEE_BPS = args.fee_bps
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    cfg, cfg_src = B._load_config(args.config)
    P = B._resolve_params(cfg)
    arm_dsls = build_dsls(P)

    now_ms = int(time.time() * 1000)
    end_ms = args.end_ms or (now_ms // MS_5M) * MS_5M - MS_5M
    start_ms = end_ms - args.days * MS_DAY

    print(f"config: {cfg_src}")
    print(f"coins: {','.join(coins)}  days: {args.days}  notional: ${args.notional:,.0f}")
    print(f"fee round-trip: {args.fee_bps} bps  funding: {'OFF' if args.no_funding else 'HL hourly ON'}")
    for arm in ARMS:
        t_ = arm_dsls[arm]["trend"]
        n_ = arm_dsls[arm]["non_trend"]
        print(f"  {arm:5s} trend hard={t_.hard_timeout_minutes:.0f}/stale="
              f"{t_.stale_flat_timeout_minutes:.0f}/stop={t_.max_loss_pct}  "
              f"non hard={n_.hard_timeout_minutes:.0f}/stale="
              f"{n_.stale_flat_timeout_minutes:.0f}/stop={n_.max_loss_pct}")

    # 预取资金费（每币一次，覆盖全窗口；带缓存）。各币 HL 上线时间不同 → 记录
    # 每币 funding 起点，回放时把该币窗口左缘裁剪到 funding 起点，杜绝上线前
    # funding 缺失被当成 0（会系统性高估长持多单）。
    funding: Dict[str, List[Tuple[int, float]]] = {}
    coin_start_ms: Dict[str, int] = {}
    if not args.no_funding:
        for coin in coins:
            t0 = time.time()
            try:
                funding[coin] = fetch_funding_hourly(
                    coin, start_ms - MS_1H, end_ms + MS_1H)
                earliest = funding[coin][0][0] if funding[coin] else start_ms
                # 资金费起点之后再留 1 根 1h 结算，避免边界计数
                coin_start_ms[coin] = max(start_ms, earliest + MS_1H)
                span_d = (funding[coin][-1][0] - earliest) / MS_DAY
                print(f"  funding {coin}: {len(funding[coin])} pts, "
                      f"{span_d:.0f}d since {time.strftime('%Y-%m-%d', time.gmtime(earliest/1000))} "
                      f"({time.time()-t0:.1f}s)")
            except Exception as e:
                print(f"  [funding-warn] {coin}: {e!r} — 该币资金费按 0 计，结果偏乐观")
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
              f"{time.strftime('%Y-%m-%d', time.gmtime(c_start/1000))} ...")
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

    # ── 报告 ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("臂级对照（pnl_nf = 扣手续费 + 含资金费）")
    print("=" * 96)
    for arm in ARMS:
        ts = [t for t in all_trades if t["arm"] == arm]
        s_nf = summ(ts, "pnl_nf")
        s_pre = summ(ts, "pnl_net")
        if s_nf["n"] == 0:
            print(f"  {ARM_LABEL[arm]:<34s} 0 笔")
            continue
        pf = f"{s_nf['pf']:.2f}" if s_nf['pf'] != float("inf") else "inf"
        print(f"  {ARM_LABEL[arm]:<34s} n={s_nf['n']:>4d} 胜率{s_nf['win']:>5.1f}% "
              f"期望${s_nf['exp']:>7.2f} 净${s_nf['net']:>9.1f} PF{pf:>5s} "
              f"均持{s_nf['hold_h']:>5.1f}h")
        print(f"    {'(含资金费)':<34s} funding合计 ${s_nf['fund']:>8.1f}   "
              f"未计funding净 ${s_pre['net']:>9.1f}  "
              f"(funding 边际 ${s_nf['net']-s_pre['net']:>+7.1f})")

    # 退出原因 × 臂
    print("\n" + "=" * 96)
    print("退出原因分解（含资金费净PnL）")
    print("=" * 96)
    reasons = sorted({t["exit_reason"] for t in all_trades})
    for arm in ARMS:
        print(f"  [{arm}]")
        for rsn in reasons:
            ts = [t for t in all_trades if t["arm"] == arm and t["exit_reason"] == rsn]
            if not ts:
                continue
            s = summ(ts)
            print(f"    {rsn:<20s} n={s['n']:>4d} 净${s['net']:>9.1f} "
                  f"期望${s['exp']:>7.2f} 均持{s['hold_h']:>6.1f}h "
                  f"fund${s['fund']:>7.1f}")

    # 持仓时长分段归因（核心问题：edge 来自哪几段时长）
    print("\n" + "=" * 96)
    print("持仓时长分段归因（含资金费）—— edge 来自哪几段？")
    print("=" * 96)
    for arm in ARMS:
        print(f"  [{arm}] {ARM_LABEL[arm]}")
        for lo, hi, label in HOLD_BUCKETS:
            ts = [t for t in all_trades if t["arm"] == arm
                  and lo <= t["hold_min"] / 60.0 < hi]
            if not ts:
                print(f"    {label:>6s}  n=0")
                continue
            s = summ(ts)
            print(f"    {label:>6s}  n={s['n']:>4d} 净${s['net']:>9.1f} "
                  f"期望${s['exp']:>7.2f} 胜率{s['win']:>5.1f}% "
                  f"fund${s['fund']:>7.1f}")

    # block bootstrap（按日聚合 Δ net，prod vs h600 / hold）
    print("\n" + "=" * 96)
    print("Block bootstrap（按日总PnL 差 vs prod，10000 次，95% CI）")
    print("=" * 96)
    import random
    random.seed(20261012)

    def daily(arm):
        d: Dict[int, float] = {}
        for t in all_trades:
            if t["arm"] == arm:
                d[t["entry_t"] // MS_DAY] = d.get(t["entry_t"] // MS_DAY, 0.0) + t["pnl_nf"]
        return d

    db = daily("prod")
    days = sorted(db)
    for arm in ("h600", "hold"):
        dw = daily(arm)
        diffs = [dw.get(d, 0.0) - db.get(d, 0.0) for d in days]
        obs = sum(diffs)
        nd = len(diffs)
        boots = []
        for _ in range(10000):
            boots.append(sum(diffs[random.randrange(nd)] for _ in range(nd)))
        boots.sort()
        lo, hi = boots[250], boots[9750]
        verdict = "显著为正" if lo > 0 else ("显著为负" if hi < 0 else "不显著")
        print(f"  {arm:5s} vs prod: Δ净=${obs:+.1f} over {nd}d  "
              f"95%CI [${lo:+.1f}, ${hi:+.1f}] → {verdict}")

    if args.write:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            f.write(json.dumps({"type": "run_meta", "config_src": cfg_src,
                                "coins": coins, "days": args.days,
                                "start_ms": start_ms, "end_ms": end_ms,
                                "funding_on": not args.no_funding,
                                "notional": args.notional,
                                "fee_bps": args.fee_bps}) + "\n")
            for t in all_trades:
                f.write(json.dumps(t) + "\n")
        print(f"\n[write] {len(all_trades)} trades -> {out_path}")


if __name__ == "__main__":
    main()
