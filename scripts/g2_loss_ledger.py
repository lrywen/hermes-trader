#!/usr/bin/env python3
"""G2 五段损耗拆解（W3/M2）——把 81 币重放的毛利→净利链条拆成五段。

输入：B-3 的逐笔 JSONL（默认 logs/b3_81coin_filt_ra.jsonl，gitignored，由
scripts/bt_ra_exch.py 的 81 币并行重放产生；可用 --trades 覆盖）。
单位：bps / 笔，of notional（每笔等权 notional=10000）。

回测每笔成交口径（scripts/bt_ra_exch.py:_emit）：
  entry_px 已含入场滑点；exit_px = raw_px*(1 - sgn*exit_slip/1e4)，权威 stop_delay=0；
  pnl_gross 含双边滑点、不含手续费；pnl_net = pnl_gross - notional*8.64bps。

严格加性瀑布（恒等残差实测 0.000 bps）：
  S0 信号峰值毛利 = peak_pct（持仓期最大有利偏移，相对含滑点入场，零成本理想值）
  L1 出场回吐     = raw 出场收益 - 峰值收益（DSL 跟踪回吐/止损触发，不含滑点费）
  L2 出场滑点     = -exit_slip（per-coin 半价差）
  L3 手续费       = -8.64 bps
  净 = S0 + L1 + L2 + L3 = pnl_net/notional
入场滑点已隐含在共同基准 entry_px（同时压低 peak 与 raw_ret，不进二者之差），
故不进净值恒等，仅名义列出。L4 穿透作为 L1 的 tail 单独统计；L5 并发占用走
block_bootstrap --maxc 对照（不在本数据内）。详见
docs/research/2026-09/g2_loss_ledger_2026-09-20.md。
"""
from __future__ import annotations

import argparse
import collections
import statistics
from typing import Any

STOP_REASONS = ("floor_breach", "exchange_trigger", "max_loss")


def analyze(path: str) -> None:
    meta: dict[str, Any] | None = None
    agg: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: collections.defaultdict(float))
    counts: collections.Counter[str] = collections.Counter()
    stop: dict[str, list[float]] = collections.defaultdict(list)

    with open(path) as fh:
        for line in fh:
            d = __import__("json").loads(line)
            kind = d.get("type")
            if kind == "run_meta":
                meta = d
                continue
            if kind != "trade":
                continue
            arm, coin = d["arm"], d["coin"]
            sgn = 1.0 if d["side"] == "long" else -1.0
            notional = d["notional"]
            slip = (meta["per_coin_slip_bps"].get(coin, 0.31)
                    if meta and meta.get("slip_mode") == "per-coin" else 0.31)

            gross_bps = d["pnl_gross"] / notional * 1e4
            net_bps = d["pnl_net"] / notional * 1e4
            peak_bps = d["peak_pct"] / 100.0 * 1e4
            # gross 已扣出场滑点（多头 -slip），加回得到 raw 出场口径
            raw_ret_bps = gross_bps + sgn * slip
            giveback = raw_ret_bps - peak_bps
            fee = gross_bps - net_bps

            a = agg[arm]
            a["S0_peak"] += peak_bps
            a["L1_giveback"] += giveback
            a["L2_xslip"] += -slip
            a["L3_fee"] += -fee
            a["net"] += net_bps
            a["resid"] += net_bps - (peak_bps + giveback - sgn * slip - fee)
            counts[arm] += 1
            if d["exit_reason"] in STOP_REASONS:
                stop[arm].append(raw_ret_bps)

    print(f"{'arm':12s} {'n':>6s} {'S0峰值':>8s} {'L1回吐':>8s} {'L2出滑':>7s} "
          f"{'L3费':>6s} {'=净':>8s} {'残差':>7s}")
    for arm in sorted(agg, key=lambda k: agg[k]["net"] / counts[k], reverse=True):
        a, c = agg[arm], counts[arm]
        print(f"{arm:12s} {c:6d} {a['S0_peak']/c:8.1f} {a['L1_giveback']/c:8.1f} "
              f"{a['L2_xslip']/c:7.2f} {a['L3_fee']/c:6.2f} {a['net']/c:8.2f} "
              f"{a['resid']/c:7.3f}")

    print("\nstop 类笔 raw_ret bps（L4 穿透 tail）")
    for arm in ("filt", "filt_exch", "filt_ra"):
        v = sorted(stop.get(arm, []))
        if not v:
            continue
        m = len(v)
        tail = sum(1 for x in v if x < -100)
        print(f"{arm:12s} n={m} mean={statistics.mean(v):7.1f} p10={v[m // 10]:7.1f} "
              f"p50={v[m // 2]:7.1f} min={v[0]:8.1f} | raw<-100bps: {tail / m * 100:.1f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="logs/b3_81coin_filt_ra.jsonl")
    analyze(ap.parse_args().trades)
