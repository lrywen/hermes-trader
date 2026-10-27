#!/usr/bin/env python3
"""maker v2 参数（base_fill_prob / adv_drift_kappa）真实 K线经验标定（离线）。

数据约束（务必先读）：
  本机无网络、无 1m K线缓存、本地 maker 账本为空；可用的是 logs/binance_klines_cache
  下连续无缺口的真实 Binance K线（最小粒度 5m，2023-05→2026-10）。因此本标定用
  **真实 5m K线**在 maker 挂单语义上做经验统计，并按 30 分钟目标 resting 时间把
  TTL 折算成根数。结论是 *5m 粒度近似*：
    - 一根 5m 内的"触及即穿/插针回收"无法分辨，touch 判定偏乐观（影线触到就算）；
    - 队列穿透完全不可观测，只能用"touch 后价格是否继续朝不利方向走"来界定
      runaway（真正成交概率的下界证据），base 因此给区间而非点值。
  不能把 5m 结果冒充 1m 精度；上线前应在拿到 1m K线后重跑本脚本。

方法（买单，offset=5bp，对称可加卖单）：
  在每个不重叠的挂单窗口（长度 ttl_bars+1）：
    post_mid = 首根收盘；limit = mid*(1-offset)；
    first_touch_k = 之后第一根 low<=limit 的相对根号；
    若窗口内不触 → 记 no_touch（真实 maker 必不成交，v1 同样不成交）；
    触及则记 touch，并看"触后到窗口末"的最低价：
       runaway = 触及后价格未再回到 limit 上方且继续下行超过 runaway_bp
                 → 被动成交大概率是逆选择（接刀），队列穿透概率应低；
       benign  = 触及后收回 limit 上方 → 良性限价成交。
    adverse_drift_bps = (limit - post_touch_next_mid)/limit*1e4（正=不利）。

输出：
  - touch_rate、first_touch 根号分布；
  - 在 touch 样本中 runaway 占比 p_adv（→ base 的下界证据）与 benign 占比；
  - adverse drift 经验分布（中位/分位），据此给 kappa 的保守取值；
  - base_fill_prob 的 Wilson 95% 区间（以 benign touch 占 touch 为"应成交"代理）；
  - 与 v2 默认值(0.70 / 1.0)对照，给出保守建议。

用法：
  python scripts/maker_v2_calibrate_real.py logs/binance_klines_cache/BTCUSDT_5m.json
  python scripts/maker_v2_calibrate_real.py <file> --offset-bps 5 --resting-min 30 \
      --runaway-bp 8 --bootstrap 2000
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from typing import Optional

from hermes_trader.execution.maker_shadow import (
    V2_DEFAULT_ADV_DRIFT_KAPPA,
    V2_DEFAULT_BASE_FILL_PROB,
)
from scripts.maker_fill_model_v2_replay import _load_candle_file

# 5m 粒度下"触及后继续跑掉"的判定阈值（bps）。低于该幅度视为噪声/收回。
DEFAULT_RUNAWAY_BP = 8.0
RESTING_MIN = 30.0


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def quantile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def collect_windows(bars, interval_min, offset_bps, resting_min, runaway_bp):
    ttl_bars = max(2, round(resting_min / interval_min))
    win = ttl_bars + 1
    off = offset_bps / 1e4
    rows = []
    for start in range(0, len(bars) - win, win):
        w = bars[start:start + win]
        if len(w) < win:
            break
        mid = w[0].c
        limit = mid * (1.0 - off)
        first = None
        for k in range(1, win):
            if w[k].l <= limit:
                first = k
                break
        if first is None:
            rows.append({"touch": False})
            continue
        # 触及后（含当根）到窗口末的最低价，判定是否继续朝不利方向跑。
        post_low = min(b.l for b in w[first:])
        move_bp = (limit - post_low) / limit * 1e4
        runaway = move_bp >= runaway_bp
        # 触及后一根 mid 代理（无下一根则用当根收盘）。
        nxt = w[first + 1] if first + 1 < win else w[first]
        post_mid = (nxt.h + nxt.l) / 2.0
        adv = (limit - post_mid) / limit * 1e4
        # 良性：触及后窗口末收回 limit 上方。
        reclaimed = w[-1].c >= limit
        rows.append({
            "touch": True, "first_k": first,
            "adverse_bp": adv, "runaway": runaway,
            "reclaimed": reclaimed, "move_bp": move_bp,
        })
    return rows, ttl_bars


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candles", help="真实 K线文件（Binance list 或 dict 布局）")
    ap.add_argument("--offset-bps", type=float, default=5.0)
    ap.add_argument("--resting-min", type=float, default=RESTING_MIN)
    ap.add_argument("--runaway-bp", type=float, default=DEFAULT_RUNAWAY_BP,
                    help="5m 粒度下触及后继续下行多少 bp 判为 runaway/逆选择")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    interval_min, bars = _load_candle_file(args.candles)
    if interval_min > 1.5:
        granularity_warning = (
            f"interval={interval_min:g}m > 1m; intrabar touch/queue unobservable, "
            "results are a COARSE lower/upper bound, not 1m-precision")
    else:
        granularity_warning = "interval=1m"

    rows, ttl_bars = collect_windows(
        bars, interval_min, args.offset_bps, args.resting_min, args.runaway_bp)
    n = len(rows)
    touches = [r for r in rows if r["touch"]]
    n_touch = len(touches)
    no_touch = n - n_touch
    runaway = [r for r in touches if r["runaway"]]
    reclaimed = [r for r in touches if r["reclaimed"]]
    adv = [r["adverse_bp"] for r in touches]
    adv_pos = [x for x in adv if x > 0]
    first_ks = [r["first_k"] for r in touches]

    # "应成交"代理 = 触及且收回（良性）；runaway 视为队列大概率未穿透/逆选择。
    benign = [r for r in touches if r["reclaimed"] and not r["runaway"]]
    p_touch = n_touch / n if n else float("nan")
    # base 是"在 touch 条件下被良性成交"的条件概率区间。
    base_lo, base_hi = wilson_ci(len(benign), n_touch)
    touch_lo, touch_hi = wilson_ci(n_touch, n)
    # 无条件（含 no-touch）真实成交率上界 ≈ touch_rate（v1 口径），下界 ≈ benign/n。
    fill_lo, fill_hi = wilson_ci(len(benign), n)

    # kappa：用 adverse drift 的经验分布。κ 目标是让"已计入价格的折扣"覆盖中位
    # 逆选择；保守取 adv 正值的 50–75 分位 / 同分布的比，这里直接给 drift 分位，
    # κ 建议维持 1.0（全额）当 drift 中位显著为正时。
    adv_med = statistics.median(adv) if adv else float("nan")
    adv_pos_med = statistics.median(adv_pos) if adv_pos else float("nan")

    # bootstrap 块重采样（按窗口块，保留一定自相关）给 runaway 率与 drift 中位 CI。
    rng = random.Random(args.seed)
    block = max(1, ttl_bars)
    indexed = touches
    boots_run, boots_adv = [], []
    if indexed and args.bootstrap > 0:
        for _ in range(args.bootstrap):
            samp = []
            while len(samp) < len(indexed):
                i = rng.randrange(0, max(1, len(indexed) - 1))
                samp.extend(indexed[i:i + block])
            samp = samp[:len(indexed)]
            boots_run.append(sum(1 for r in samp if r["runaway"]) / len(samp))
            a = [r["adverse_bp"] for r in samp if r["adverse_bp"] > 0]
            if a:
                boots_adv.append(statistics.median(a))

    def bci(xs):
        if not xs:
            return (None, None)
        return (round(quantile(xs, 0.025), 4), round(quantile(xs, 0.975), 4))

    out = {
        "source": args.candles,
        "granularity_warning": granularity_warning,
        "interval_min": interval_min,
        "ttl_bars": ttl_bars,
        "offset_bps": args.offset_bps,
        "runaway_bp": args.runaway_bp,
        "n_windows": n,
        "touch": {
            "n": n_touch, "rate": round(p_touch, 4),
            "rate_wilson95": [round(touch_lo, 4), round(touch_hi, 4)],
            "no_touch": no_touch,
            "first_touch_bars_p50": quantile(first_ks, 0.5),
            "first_touch_bars_p90": quantile(first_ks, 0.9),
        },
        "conditional_on_touch": {
            "runaway_n": len(runaway),
            "runaway_rate": round(len(runaway) / n_touch, 4) if n_touch else None,
            "runaway_rate_boot95": bci(boots_run),
            "reclaimed_n": len(reclaimed),
            "benign_n": len(benign),
            "base_fill_prob_point": round(len(benign) / n_touch, 4) if n_touch else None,
            "base_fill_prob_wilson95": [round(base_lo, 4), round(base_hi, 4)],
        },
        "unconditional_fill": {
            "v1_touch_fill_rate": round(p_touch, 4),
            "benign_fill_rate": round(len(benign) / n, 4) if n else None,
            "benign_fill_wilson95": [round(fill_lo, 4), round(fill_hi, 4)],
        },
        "adverse_drift_bps": {
            "median_all": None if adv_med != adv_med else round(adv_med, 3),
            "median_positive": None if adv_pos_med != adv_pos_med else round(adv_pos_med, 3),
            "p25": round(quantile(adv, 0.25), 3),
            "p75": round(quantile(adv, 0.75), 3),
            "p90": round(quantile(adv, 0.90), 3),
            "median_positive_boot95": bci(boots_adv),
            "advise_kappa": (
                1.0 if (adv_med == adv_med and adv_med > 0) else 0.0),
        },
        "defaults": {
            "v2_default_base": V2_DEFAULT_BASE_FILL_PROB,
            "v2_default_kappa": V2_DEFAULT_ADV_DRIFT_KAPPA,
        },
    }

    # 建议：base 取 Wilson 上界与默认 0.30 的较小者（保守，不超过经验上界）。
    base_hi_v = out["conditional_on_touch"]["base_fill_prob_wilson95"][1]
    kappa_rec = out["adverse_drift_bps"]["advise_kappa"]
    note = ("use Wilson/bootstrap interval bounds, not point estimates; queue "
            "penetration is unobservable from OHLC and needs real fill reports")
    if interval_min > 1.5:
        note = ("coarse >1m estimate; intrabar wicks overstate drift; "
                "rerun on 1m before live shadow v2 promote; ") + note
    out["recommendation"] = {
        "base_fill_prob_conservative": (
            round(min(base_hi_v, V2_DEFAULT_BASE_FILL_PROB), 4)
            if base_hi_v == base_hi_v else V2_DEFAULT_BASE_FILL_PROB),
        "adv_drift_kappa": kappa_rec if kappa_rec == kappa_rec else 1.0,
        "note": note,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 74)
    print("maker v2 empirical calibration on REAL candles (offline, no funds)")
    print(f"source={args.candles}")
    print(f"WARNING: {granularity_warning}")
    print(f"offset={args.offset_bps}bp resting={args.resting_min}m "
          f"(ttl={ttl_bars} bars) runaway>={args.runaway_bp}bp windows={n}")
    print("-" * 74)
    t = out["touch"]; c = out["conditional_on_touch"]; a = out["adverse_drift_bps"]
    print(f"touch rate (v1 fill upper bound): {t['rate']} "
          f"Wilson95={t['rate_wilson95']}  first-touch bars p50={t['first_touch_bars_p50']} p90={t['first_touch_bars_p90']}")
    print(f"on-touch runaway/adverse sel rate: {c['runaway_rate']} boot95={c['runaway_rate_boot95']}")
    print(f"benign(touch&reclaim) rate       : {c['base_fill_prob_point']} "
          f"Wilson95={c['base_fill_prob_wilson95']}")
    print(f"unconditional benign fill rate   : {out['unconditional_fill']['benign_fill_rate']} "
          f"Wilson95={out['unconditional_fill']['benign_fill_wilson95']}")
    print(f"adverse drift bps: med={a['median_all']} med+={a['median_positive']} "
          f"p75={a['p75']} p90={a['p90']} med+ boot95={a['median_positive_boot95']}")
    print("-" * 74)
    print(f"defaults: base={V2_DEFAULT_BASE_FILL_PROB} kappa={V2_DEFAULT_ADV_DRIFT_KAPPA}")
    print(f"RECOMMEND conservative: base={out['recommendation']['base_fill_prob_conservative']} "
          f"kappa={out['recommendation']['adv_drift_kappa']}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
