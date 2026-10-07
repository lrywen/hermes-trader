#!/usr/bin/env python3
"""Funding-carry 极端拥挤信号的 shadow forward 监控（三口径）。

背景
----
funding_carry_eval 发现：|每小时 funding|>=0.0003（极端拥挤）后反向持 72h，
理论 delta-neutral 口径 +134bp/win96%。但 HL 现货对极少（26 主流 perp 中仅
TRUMP/HYPE 有现货），无法在 HL 内普遍用现货对冲。为在真实约束下检验 edge，
本监控对每个信号同时记录三种可执行口径，72h 后结算：

1. ``theoretical_neutral``：票息(实际后续 funding)+basis(premium 收敛)，
   成本 32bp。理想基准（多数币无法落地）。
2. ``perp_hedge``：用 HL 内**滚动相关最高的另一 perp** 当对冲腿；收益=
   信号腿(票息+价格变动) − 对冲腿价格变动；两腿 perp taker 开+平 18bp。
   这是 HL 内真实可执行的近似中性。
3. ``bare_directional``：反向裸单价格收益 − 9bp（对照，预期无 edge）。

运行
----
每次执行：扫描面板最新 funding 产生新信号 -> 对到期(>=hold_h)信号结算 ->
打印滚动汇总。状态存 ``${HERMES_DATA_DIR}/funding-carry-forward.json``，
明细（每条信号，含 open/settled）追加 ``...jsonl``。设计为由 scheduler.py
每小时调用一次（HH:25），可安全重复运行（按 coin+signal_hour 去重）。

只读公开端点、只写可再生成的 forward 文件；不加载私钥、不下单。
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

HOLD_H_DEFAULT = 72
TH_DEFAULT = 0.0003
COST_NEUTRAL = 32.0
COST_HEDGE = 18.0     # 两 perp 腿 开+平 4 次 taker ×4.5
COST_BARE = 9.0
HEDGE_LOOKBACK_H = 24 * 30   # 对冲腿相关性估计窗口
HEDGE_MIN_CORR = 0.2
_HOUR_MS = 3_600_000


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_DATA_DIR", "/data"))


def _funding_root() -> Path:
    return _data_dir() / "funding"


def load_coin(coin: str) -> list[dict]:
    p = _funding_root() / f"{coin}.jsonl.gz"
    if not p.exists():
        return []
    return [json.loads(x) for x in gzip.open(p, "rt") if x.strip()]


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"version": 1, "signals": []}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    os.replace(tmp, path)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 24:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return sxy / math.sqrt(sxx * syy)


def _returns(closes: list[float]) -> list[float]:
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0]


def choose_hedge(coin: str, signal_time: int, coins_panel: list[str],
                 price_closes: dict[str, list[float]],
                 price_times: dict[str, list[int]]) -> tuple[str, float] | None:
    """选 signal_time 之前 HEDGE_LOOKBACK_H 内收益相关性最高的另一 perp。

    返回 (coin, corr)。极端 funding 多出现于币独立异动，相关性天然偏低，故
    阈值放宽（HEDGE_MIN_CORR），并把实际相关性交调用方记录，供事后按对冲
    质量分层。找不到（连最低相关都不达标）返回 None。
    """
    sig_r = _returns(_slice_closes(coin, signal_time, price_times, price_closes))
    best, best_c = None, HEDGE_MIN_CORR
    for other in coins_panel:
        if other == coin:
            continue
        o_r = _returns(_slice_closes(other, signal_time, price_times, price_closes))
        c = _pearson(sig_r, o_r)
        if c >= best_c:
            best, best_c = other, c
    return (best, best_c) if best else None


def _slice_closes(coin: str, t_end: int,
                  price_times: dict[str, list[int]],
                  price_closes: dict[str, list[float]]) -> list[float]:
    ta = price_times.get(coin, [])
    ca = price_closes.get(coin, [])
    j = 0
    while j < len(ta) and ta[j] <= t_end:
        j += 1
    lo_t = t_end - HEDGE_LOOKBACK_H * _HOUR_MS
    i = j
    while i > 0 and ta[i - 1] >= lo_t:
        i -= 1
    return ca[i:j]


def settle(sig: dict, panel: dict[str, list[dict]],
           price_times: dict[str, list[int]],
           price_closes: dict[str, list[float]]) -> bool:
    """对到期信号计算三口径。返回是否成功结算。"""
    coin = sig["coin"]
    rows = panel[coin]
    exit_time = sig["time"] + sig["hold_h"] * _HOUR_MS
    if not rows or rows[-1]["time"] < exit_time:
        return False
    # 找到 i（信号行）与 exit 行
    i = next((k for k, r in enumerate(rows) if r["time"] == sig["time"]), None)
    e = next((k for k, r in enumerate(rows) if r["time"] >= exit_time), None)
    if i is None or e is None or e <= i:
        return False
    fr0 = float(rows[i]["fundingRate"])
    sign = -1 if fr0 > 0 else 1
    cum_funding = sum(float(rows[k]["fundingRate"])
                      for k in range(i + 1, e + 1))
    coupon = (cum_funding if fr0 > 0 else -cum_funding) * 1e4
    p0, p1 = float(rows[i]["premium"]), float(rows[e]["premium"])
    basis = ((p0 - p1) if fr0 > 0 else (p1 - p0)) * 1e4

    # 价格：信号腿入场/出场（用 funding 时间最接近的 1h 收盘）
    entry_px = _px_at(coin, sig["time"], price_times, price_closes)
    exit_px = _px_at(coin, exit_time, price_times, price_closes)
    leg_ret = ((exit_px - entry_px) / entry_px * 1e4
               if entry_px and exit_px else 0.0)

    sig["theoretical_neutral"] = round(coupon + basis - COST_NEUTRAL, 2)

    # perp hedge
    hc = sig.get("hedge_coin")
    if hc:
        h0 = _px_at(hc, sig["time"], price_times, price_closes)
        h1 = _px_at(hc, exit_time, price_times, price_closes)
        if h0 and h1:
            h_ret = (h1 - h0) / h0 * 1e4
            # 信号腿 PnL = sign 方向(价格 + 票息)；对冲腿 = -sign 方向价格
            pnl = sign * leg_ret + coupon - sign * h_ret - COST_HEDGE
            sig["perp_hedge"] = round(pnl, 2)

    sig["bare_directional"] = round(sign * leg_ret - COST_BARE, 2)
    sig["settled_at"] = int(time.time() * 1000)
    sig["status"] = "settled"
    return True


def _px_at(coin, t, price_times, price_closes):
    ta = price_times.get(coin, [])
    ca = price_closes.get(coin, [])
    if not ta:
        return None
    lo, hi = 0, len(ta) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if ta[mid] <= t:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        return None
    return ca[best] if ca[best] > 0 else None


def _load_prices(coins: list[str]):
    """按需取各币最近 ~75 天的 1h 收盘（historical_candles，走缓存）。

    只需覆盖：对冲相关性估计(30d) + 最长持有(72h) + 结算缓冲；不向远史拉取，
    避开 fetch_candle_range 的 20k-bar 单请求守卫。
    """
    from hermes_trader.data import historical_candles as hc
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 75 * 24 * _HOUR_MS
    ptimes, pcloses = {}, {}
    for c in coins:
        try:
            bars = hc.fetch_candle_range(c, "1h", start_ms, now_ms)
            ptimes[c] = [b.t for b in bars]
            pcloses[c] = [b.c for b in bars]
        except Exception:
            ptimes[c], pcloses[c] = [], []
    return ptimes, pcloses


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--th", type=float, default=TH_DEFAULT)
    ap.add_argument("--hold-h", type=int, default=HOLD_H_DEFAULT)
    ap.add_argument("--no-hedge", action="store_true",
                    help="跳过 perp 对冲腿选择/结算（更快）")
    ap.add_argument("--state", default=None)
    args = ap.parse_args()

    state_path = Path(args.state) if args.state else \
        _data_dir() / "funding-carry-forward.json"
    root = _funding_root()
    coin_files = sorted(p.name.split(".")[0]
                        for p in root.glob("*.jsonl.gz"))
    panel = {c: load_coin(c) for c in coin_files}
    panel = {c: sorted(r, key=lambda r: r["time"])
             for c, r in panel.items() if r}

    state = load_state(state_path)
    existing = {(s["coin"], s["sig_hour"]) for s in state["signals"]}

    # 1) 扫描新信号：取每币最新 funding 行
    new_sigs = []
    for c, rows in panel.items():
        last = rows[-1]
        fr = float(last["fundingRate"])
        if abs(fr) < args.th:
            continue
        # 用整点小时做去重键
        sig_hour = (last["time"] // _HOUR_MS) * _HOUR_MS
        if (c, sig_hour) in existing:
            continue
        sig = {"coin": c, "time": last["time"], "sig_hour": sig_hour,
               "fundingRate": fr, "premium": float(last["premium"]),
               "hold_h": args.hold_h, "opened_at": int(time.time() * 1000),
               "status": "open"}
        state["signals"].append(sig)
        new_sigs.append(sig)

    # 2) 选对冲腿（需要价格）；对未结算且尚未成功选定(None=未找到/取价失败)的
    # 信号持续重试，避免一次 429/取价失败后永久放弃对冲口径。
    need_hedge = [s for s in state["signals"]
                  if s["status"] == "open" and not s.get("hedge_coin")]
    if not args.no_hedge and (new_sigs or need_hedge):
        ptimes, pcloses = _load_prices(list(panel))
        for s in need_hedge:
            picked = choose_hedge(s["coin"], s["time"], list(panel),
                                 pcloses, ptimes)
            if picked:
                s["hedge_coin"], s["hedge_corr"] = picked[0], round(picked[1], 3)
            else:
                s["hedge_coin"] = None
    else:
        ptimes = pcloses = {}

    # 3) 结算到期信号
    if ptimes or args.no_hedge:
        if args.no_hedge:
            ptimes, pcloses = _load_prices(list(panel))
        for s in state["signals"]:
            if s["status"] == "open":
                settle(s, panel, ptimes, pcloses)

    save_state(state_path, state)

    # 4) 汇总
    settled = [s for s in state["signals"] if s["status"] == "settled"]
    print(f"panel coins {len(panel)} | new signals {len(new_sigs)} | "
          f"open {sum(1 for s in state['signals'] if s['status']=='open')} "
          f"settled {len(settled)}")
    for key in ("theoretical_neutral", "perp_hedge", "bare_directional"):
        xs = [s[key] for s in settled if key in s]
        if xs:
            print(f"  {key:<20} n={len(xs):>4} mean {sum(xs)/len(xs):7.1f} "
                  f"win {sum(1 for z in xs if z>0)/len(xs):.3f}")
    if new_sigs:
        print("new:")
        for s in new_sigs:
            print(f"  {s['coin']:<10} fr={s['fundingRate']:.5f} "
                  f"hedge={s.get('hedge_coin')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
